"""Piezas comunes de los kernels (plan §3.3, RFC-0001).

Cada kernel es una función pura ``kernel(program, params, dtype=COMPUTE_DTYPE) -> Outputs``
que devuelve **expresiones** Polars (una por salida, clave = sufijo de salida, ``""`` =
salida única). Los resultados intermedios reutilizados se ligan a columnas temporales con
:meth:`Program.let` para que los árboles de expresiones no crezcan exponencialmente (un
``ewm_seeded`` referencia su entrada cuatro veces; ADX encadena tres). El motor aplica el
:class:`Program` por niveles de dependencia y evalúa todo en **una única consulta**.

Convenciones compartidas (RF-3):

- Las entradas ``Float32`` se convierten a ``dtype`` (``Float64`` en producción) y los no
  finitos (``NaN``, ``±inf``) se tratan como nulos.
- Los recursivos trabajan sobre la **subsecuencia de filas válidas** (todas sus entradas no
  nulas): la fila nula recibe salida nula y no altera el estado.
- Los de memoria finita devuelven nulo si su ventana contiene alguna fila nula.

``dtype`` solo se cambia en los tests de alcanzabilidad de la tabla ``f32`` (T2.9).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import polars as pl

#: Tipo de cálculo de los kernels (RFC-0001): único lugar, junto con ``engine.py``.
COMPUTE_DTYPE: type[pl.DataType] = pl.Float64
#: Prefijo de las columnas temporales (nunca llegan a la salida).
TEMP_PREFIX = "__ft_"

ParamValue = int | float | str
Params = Mapping[str, ParamValue]
#: Salidas de un kernel: sufijo de salida (``""`` = salida única) → expresión.
Outputs = dict[str, pl.Expr]


@dataclass
class _Store:
    """Definiciones compartidas por un programa y sus hijos."""

    definitions: dict[str, tuple[int, pl.Expr]] = field(default_factory=dict)
    inputs: dict[tuple[str, str], pl.Expr] = field(default_factory=dict)


class Program:
    """Columnas intermedias con nombre, ordenadas por nivel de dependencia."""

    def __init__(self, scope: str = "k", store: _Store | None = None) -> None:
        self._scope = scope
        self._store = store if store is not None else _Store()

    def child(self, scope: str) -> Program:
        """Programa con otro ámbito de nombres que comparte definiciones y entradas."""
        return Program(f"{self._scope}_{scope}", self._store)

    def let(self, expr: pl.Expr) -> pl.Expr:
        """Liga ``expr`` a una columna temporal y devuelve la referencia ``pl.col``."""
        definitions = self._store.definitions
        name = f"{TEMP_PREFIX}{self._scope}_{len(definitions)}"
        level = 1 + max(
            (definitions[r][0] for r in expr.meta.root_names() if r in definitions), default=0
        )
        definitions[name] = (level, expr.alias(name))
        return pl.col(name)

    def bound(self, expr: pl.Expr) -> pl.Expr:
        """``expr`` si ya es una referencia a columna; si no, :meth:`let`."""
        return expr if expr.meta.is_column() else self.let(expr)

    def float_input(self, column: str, dtype: type[pl.DataType]) -> pl.Expr:
        """Entrada convertida a ``dtype`` con ``NaN``/``±inf`` → nulo (RF-3), compartida."""
        key = (column, str(dtype))
        cached = self._store.inputs.get(key)
        if cached is None:
            value = pl.col(column).cast(dtype)
            cached = self.let(pl.when(value.is_finite()).then(value))
            self._store.inputs[key] = cached
        return cached

    def apply(self, lazy: pl.LazyFrame) -> pl.LazyFrame:
        """Añade las columnas temporales a ``lazy``, un ``with_columns`` por nivel."""
        levels: dict[int, list[pl.Expr]] = {}
        for level, expr in self._store.definitions.values():
            levels.setdefault(level, []).append(expr)
        for level in sorted(levels):
            lazy = lazy.with_columns(levels[level])
        return lazy


Kernel = Callable[..., Outputs]


def evaluate(
    frame: pl.DataFrame,
    kernel: Kernel,
    params: Params,
    dtype: type[pl.DataType] = COMPUTE_DTYPE,
) -> pl.DataFrame:
    """Evalúa un kernel aislado sobre ``frame`` (salida única → columna ``value``)."""
    program = Program()
    outputs = kernel(program, params, dtype)
    named = [expr.alias(key or "value") for key, expr in outputs.items()]
    return program.apply(frame.lazy()).select(named).collect()


def int_param(params: Params, name: str) -> int:
    """Parámetro entero ya validado por el catálogo."""
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"parámetro {name!r} debe ser entero, recibido {value!r}"
        raise TypeError(msg)
    return value


def float_param(params: Params, name: str) -> float:
    """Parámetro real ya validado por el catálogo."""
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"parámetro {name!r} debe ser numérico, recibido {value!r}"
        raise TypeError(msg)
    return float(value)


def source_column(params: Params) -> str:
    """Columna de entrada de los indicadores de una serie (``source``, default ``close``)."""
    value = params.get("source", "close")
    if not isinstance(value, str):
        msg = f"source debe ser texto, recibido {value!r}"
        raise TypeError(msg)
    return value


def all_valid(*exprs: pl.Expr) -> pl.Expr:
    """``True`` en las filas donde todas las expresiones son no nulas."""
    return pl.all_horizontal(*(e.is_not_null() for e in exprs))


def scaled(value: pl.Expr, divisor: float) -> pl.Expr:
    """``value / divisor`` para un divisor constante, como ``value · (1/divisor)``.

    Polars divide entre un escalar con dos rutas distintas según la longitud y el modo
    (división real en marcos pequeños, multiplicación por el recíproco en el resto), lo que
    rompería la estabilidad bit a bit del prefijo (RF-3, T3.1); la multiplicación es una
    única operación IEEE en cualquier ruta.
    """
    return value * (1.0 / divisor)


def null_count_in_window(valid: pl.Expr, window: int) -> pl.Expr:
    """Número de filas inválidas en la ventana de ``window`` filas que termina en ``t``.

    Es nulo mientras la ventana no está completa (calentamiento).
    """
    return (~valid).cast(pl.UInt32).rolling_sum(window)


def previous_valid(program: Program, value: pl.Expr) -> pl.Expr:
    """Último valor no nulo **anterior** a la fila (solo mira filas ``< t``)."""
    return program.let(program.bound(value).shift(1).forward_fill())


def ewm_seeded(program: Program, value: pl.Expr, period: int, alpha: float) -> pl.Expr:
    """Suavizado exponencial ``y_t = y_{t-1} + α·(x_t − y_{t-1})`` sembrado con una SMA.

    Sobre la subsecuencia de valores no nulos de ``value``: la semilla es la media simple
    de los ``period`` primeros (suma secuencial, como TA-Lib) en el ``period``-ésimo; antes
    hay nulo (calentamiento). Las filas nulas reciben nulo y no alteran el estado.
    """
    x = program.bound(value)
    position = x.cum_count() - 1  # índice entre los no nulos (válido en filas no nulas)
    staged = (
        pl.when(x.is_null() | (position < period - 1))
        .then(None)
        .when(position == period - 1)
        .then(scaled(x.cum_sum(), period))
        .otherwise(x)
    )
    return program.let(staged.ewm_mean(alpha=alpha, adjust=False, ignore_nulls=True))


def wilder_sum_seeded(program: Program, value: pl.Expr, period: int) -> pl.Expr:
    """Media de Wilder con la semilla de las sumas de ±DI/ADX de TA-Lib (``S/period``).

    TA-Lib suma los ``period−1`` primeros valores y aplica un paso de Wilder con el
    ``period``-ésimo: ``S_p = Σ(x_1..x_{p−1})·(1 − 1/p) + x_p``; después
    ``S_t = S_{t−1} − S_{t−1}/p + x_t``. En forma de media (``M = S/p``) equivale a sembrar
    ``M = Σ(x_1..x_{p−1})/p`` en el ``(p−1)``-ésimo valor y suavizar con ``α = 1/p``; la
    fila de semilla se anula (primera salida en el ``p``-ésimo valor).
    """
    x = program.bound(value)
    position = x.cum_count() - 1
    staged = (
        pl.when(x.is_null() | (position < period - 2))
        .then(None)
        .when(position == period - 2)
        .then(scaled(x.cum_sum(), period))
        .otherwise(x)
    )
    smoothed = program.let(staged.ewm_mean(alpha=1.0 / period, adjust=False, ignore_nulls=True))
    return program.let(pl.when(position == period - 2).then(None).otherwise(smoothed))


__all__ = [
    "COMPUTE_DTYPE",
    "TEMP_PREFIX",
    "Kernel",
    "Outputs",
    "ParamValue",
    "Params",
    "Program",
    "all_valid",
    "evaluate",
    "ewm_seeded",
    "float_param",
    "int_param",
    "null_count_in_window",
    "previous_valid",
    "scaled",
    "source_column",
    "wilder_sum_seeded",
]
