"""Motor de cálculo de ``ft_indicators`` (RF-1.4, RF-3, RF-16; plan §3.4).

- **Recálculo completo**: todas las expresiones de los indicadores pedidos se evalúan en una
  única consulta Polars (``select``) sobre la serie completa; misma entrada ⇒ misma salida
  (idempotencia por construcción, RF-8).
- **Alineación 1:1**: una fila de salida por fila fuente, mismo ``open_time_ms``; el
  calentamiento se conserva como nulo.
- **Frontera de precisión (RFC-0001)**: los kernels calculan en ``Float64``;
  :func:`compute` aplica el **único** ``cast`` a ``Float32`` (:data:`PERSIST_DTYPE`) antes de
  devolver lo persistible. :func:`compute_float64` expone el oráculo ``Float64`` solo para el
  paquete de *fixtures* (``oracle.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl
from finances_core.schemas.indicators import Stability

from .catalog import ResolvedIndicator
from .convergence import stability
from .kernels.common import Program

TIME_COLUMN = "open_time_ms"
#: Tipo persistido/exportado de todas las columnas de indicadores (Art. 5.1).
PERSIST_DTYPE: type[pl.DataType] = pl.Float32
#: Tipo interno de cálculo (RFC-0001); no sale de ``kernels/`` + ``engine.py``.
ORACLE_DTYPE: type[pl.DataType] = pl.Float64


@dataclass(frozen=True)
class IndicatorOutput:
    """Metadatos de un indicador calculado (para ``ComputeItemResult`` y el manifiesto)."""

    indicator: ResolvedIndicator
    stability: Stability
    warnings: tuple[str, ...]
    null_inputs: int


@dataclass(frozen=True)
class EngineResult:
    """Salida persistible: ``open_time_ms`` (``Int64``) + columnas ``Float32``."""

    frame: pl.DataFrame
    outputs: dict[str, IndicatorOutput]  # por indicator_id, en orden de petición

    def series_frame(self, indicator_id: str) -> pl.DataFrame:
        """``open_time_ms`` + columnas de un indicador (lo que se escribe en su archivo)."""
        columns = list(self.outputs[indicator_id].indicator.columns.values())
        return self.frame.select(TIME_COLUMN, *columns)


def _unique(indicators: Sequence[ResolvedIndicator]) -> list[ResolvedIndicator]:
    seen: dict[str, ResolvedIndicator] = {}
    for resolved in indicators:
        seen.setdefault(resolved.indicator_id, resolved)
    return list(seen.values())


def _check_inputs(frame: pl.DataFrame, indicators: Sequence[ResolvedIndicator]) -> None:
    required = {TIME_COLUMN, *(c for r in indicators for c in r.inputs)}
    missing = sorted(required - set(frame.columns))
    if missing:
        msg = f"Columnas de entrada ausentes para el motor: {missing}"
        raise ValueError(msg)


def _plan(
    frame: pl.DataFrame, indicators: Sequence[ResolvedIndicator]
) -> tuple[pl.LazyFrame, list[str]]:
    """Consulta ``Float64`` única con todas las salidas pedidas (sin ejecutar)."""
    _check_inputs(frame, indicators)
    program = Program()
    exprs: list[pl.Expr] = []
    columns: list[str] = []
    for index, resolved in enumerate(indicators):
        outputs = resolved.definition.kernel(
            program.child(f"i{index}"), resolved.params, ORACLE_DTYPE
        )
        for suffix, column in resolved.columns.items():
            exprs.append(outputs[suffix].alias(column))
            columns.append(column)
    return program.apply(frame.lazy()).select(pl.col(TIME_COLUMN), *exprs), columns


def compute_float64(frame: pl.DataFrame, indicators: Sequence[ResolvedIndicator]) -> pl.DataFrame:
    """Salidas ``Float64`` del oráculo (solo para ``oracle.py`` y los tests).

    Raises:
        ValueError: SI falta ``open_time_ms`` o alguna columna de entrada.
    """
    lazy, _columns = _plan(frame, _unique(indicators))
    return lazy.collect()


def _valid_times(frame: pl.DataFrame, inputs: Sequence[str]) -> list[int]:
    valid = pl.all_horizontal(*(pl.col(c).is_finite() for c in inputs))
    return frame.filter(valid.fill_null(value=False))[TIME_COLUMN].to_list()


def _first_non_null(result: pl.DataFrame, columns: Sequence[str]) -> dict[str, int | None]:
    if result.height == 0 or not columns:
        return dict.fromkeys(columns)
    row = result.select(
        pl.col(TIME_COLUMN).filter(pl.col(c).is_not_null()).first().alias(c) for c in columns
    ).row(0)
    return {c: (int(v) if v is not None else None) for c, v in zip(columns, row, strict=True)}


def compute(frame: pl.DataFrame, indicators: Sequence[ResolvedIndicator]) -> EngineResult:
    """Calcula los indicadores pedidos sobre la serie completa y los deja en ``Float32``.

    Warnings por indicador: ``insufficient_history`` (filas válidas ≤ calentamiento: la
    columna queda toda nula), ``not_converged`` (RF-16) y ``null_inputs=N`` (filas fuente con
    alguna entrada nula o no finita, RF-3).

    Raises:
        ValueError: SI falta ``open_time_ms`` o alguna columna de entrada.
    """
    unique = _unique(indicators)
    lazy, columns = _plan(frame, unique)
    persisted = lazy.with_columns(pl.col(columns).cast(PERSIST_DTYPE)).collect()

    first = _first_non_null(persisted, columns)
    outputs: dict[str, IndicatorOutput] = {}
    for resolved in unique:
        valid_times = _valid_times(frame, resolved.inputs)
        stab = stability(resolved, valid_times, first)
        warnings: list[str] = []
        if any(len(valid_times) <= w for w in resolved.warmups().values()):
            warnings.append("insufficient_history")
        if stab.reason == "not_converged":
            warnings.append("not_converged")
        null_inputs = frame.height - len(valid_times)
        if null_inputs:
            warnings.append(f"null_inputs={null_inputs}")
        outputs[resolved.indicator_id] = IndicatorOutput(
            indicator=resolved,
            stability=stab,
            warnings=tuple(warnings),
            null_inputs=null_inputs,
        )
    return EngineResult(frame=persisted, outputs=outputs)


__all__ = [
    "ORACLE_DTYPE",
    "PERSIST_DTYPE",
    "TIME_COLUMN",
    "EngineResult",
    "IndicatorOutput",
    "compute",
    "compute_float64",
]
