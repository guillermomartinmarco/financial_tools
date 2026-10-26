"""Motor de ``ft_ta_lib`` (RF-1.4, RF-6, RF-7, RF-17; plan §3.3) — frontera ``float64``.

Único módulo que **calcula** con TA-Lib (``catalog.py``/``resolver.py`` solo leen
metadatos). RFC-0002:

- Las entradas ``Float32`` se convierten a arrays NumPy ``float64`` contiguos solo para
  llamar a TA-Lib (:func:`_as_compute_array`); ``float64`` no sale de este módulo.
- Las salidas reales se convierten a ``Float32`` en un **único punto**
  (:func:`_persist_real`, ``cast(PERSIST_REAL)``); ``NaN``/``±inf`` (incluido el
  desbordamiento de ``Float32``) → nulo.
- Salidas enteras de patrón/categoría → ``Int16`` con control de rango
  (:class:`OutputOutOfRangeError`, nunca se trunca) y nulas en las filas ``< lookback``
  (TA-Lib rellena el calentamiento con 0).
- Salidas de índice → ``open_time_ms`` (``Int64``) de la fila referida.

**Nulos en entradas (RF-6, ADR-T07)**: la función se ejecuta sobre la subsecuencia de filas
con todas sus entradas válidas y las filas excluidas quedan nulas (las ventanas "saltan"
esas filas). **Recálculo completo**: misma entrada ⇒ misma salida. La configuración global
de TA-Lib (*unstable period*, compatibilidad) nunca se modifica (:func:`global_config`).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl
import structlog
import talib
import talib.abstract
from finances_core.schemas.indicators import Stability

from .errors import OutputOutOfRangeError, TaLibExecutionError
from .model import UNSTABLE_FLAG, OutputMeta, ResolvedFunction, TalibParam

logger = structlog.get_logger(__name__)

TIME_COLUMN = "open_time_ms"
#: Tipo de cálculo de TA-Lib (frontera RFC-0002; solo dentro de este módulo).
COMPUTE_DTYPE = np.float64
#: Tipo persistido de las salidas reales (Art. 5.1): único destino de la conversión.
PERSIST_REAL: type[pl.DataType] = pl.Float32
#: Rango de ``Int16`` (Art. 5.1 bis, RFC-0002).
INT16_RANGE = (-32_768, 32_767)
#: Filas mínimas de la serie sintética de prueba (plan §3.2).
MIN_PROBE_ROWS = 64

_UNSTABLE_RULE = (
    "unstable_period_unknown: TA-Lib no publica una regla analítica de convergencia "
    "(unstable period = 0)"
)


@dataclass(frozen=True)
class FunctionResult:
    """Salida persistible de una función: ``open_time_ms`` + columnas con su dtype (RF-7)."""

    resolved: ResolvedFunction
    frame: pl.DataFrame  # una fila por fila fuente (alineación 1:1)
    stability: Stability
    warnings: tuple[str, ...]  # insufficient_history, null_inputs=N
    null_inputs: int  # filas fuente excluidas por tener alguna entrada nula/no finita
    valid_rows: int  # filas de la subsecuencia calculada


# ------------------------------------------------------------------ frontera float64


def _as_compute_array(series: pl.Series) -> np.ndarray[Any, Any]:
    """Columna fuente (sin nulos) → array ``float64`` contiguo para TA-Lib."""
    return np.ascontiguousarray(series.to_numpy(), dtype=COMPUTE_DTYPE)


def _persist_real(name: str, values: np.ndarray[Any, Any]) -> pl.Series:
    """Único punto de conversión ``float64`` → ``Float32``; no finitos → nulo."""
    narrowed = pl.Series(name, values).cast(PERSIST_REAL)
    return pl.select(pl.when(narrowed.is_finite()).then(narrowed).alias(name)).to_series()


def synthetic_inputs(
    columns: Iterable[str], rows: int, *, seed: int = 20_240_101
) -> dict[str, np.ndarray[Any, Any]]:
    """Serie sintética determinista (paseo aleatorio OHLCV) para la ejecución de prueba.

    Columnas desconocidas (p. ej. ``periods`` de ``MAVP``) reciben una constante positiva.
    """
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, rows)))
    open_ = np.concatenate(([100.0], close[:-1]))[:rows]
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.003, rows)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.003, rows)))
    base = {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "adj_close": close * 0.98,
        "volume": np.abs(rng.normal(1000.0, 200.0, rows)),
    }
    out: dict[str, np.ndarray[Any, Any]] = {}
    for col in dict.fromkeys(columns):
        values = base.get(col, np.full(rows, 10.0))
        out[col] = np.ascontiguousarray(values, dtype=COMPUTE_DTYPE)
    return out


def _as_list(result: object) -> list[np.ndarray[Any, Any]]:
    if isinstance(result, list | tuple):
        return [np.asarray(a) for a in result]
    return [np.asarray(result)]


def integer_outputs(function: Any, columns: Iterable[str], rows: int) -> list[bool]:
    """Ejecuta ``function`` (``talib.abstract.Function`` o compatible) con sus defaults sobre
    la serie sintética y devuelve, por salida, si su array es entero (catálogo, plan §3.1)."""
    outputs = _as_list(function(synthetic_inputs(columns, rows)))
    return [bool(np.issubdtype(a.dtype, np.integer)) for a in outputs]


def _function(fn: ResolvedFunction) -> Any:
    """Instancia nueva de ``talib.abstract.Function`` (sin estado compartido entre hilos)."""
    function = talib.abstract.Function(fn.name)
    names: dict[str, str | list[str]] = {}
    for meta in fn.meta.inputs:
        cols = fn.inputs[meta.name]
        names[meta.name] = cols[0] if meta.single else list(cols)
    function.set_input_names(names)
    return function


def _run(
    fn: ResolvedFunction, arrays: Mapping[str, np.ndarray[Any, Any]]
) -> list[np.ndarray[Any, Any]]:
    """Llama a TA-Lib; cualquier excepción → :class:`TaLibExecutionError` con su mensaje."""
    params: dict[str, TalibParam] = dict(fn.params)
    try:
        outputs = _as_list(_function(fn)(dict(arrays), **params))
    except Exception as exc:
        msg = f"{fn.name}: {exc}"
        raise TaLibExecutionError(msg) from exc
    if len(outputs) != len(fn.meta.outputs):
        expected = len(fn.meta.outputs)
        msg = f"{fn.name}: TA-Lib devolvió {len(outputs)} salidas, se esperaban {expected}"
        raise TaLibExecutionError(msg)
    return outputs


def probe(fn: ResolvedFunction, rows: int | None = None) -> list[bool]:
    """Ejecución de prueba sobre ``max(64, 2·lookback)`` filas sintéticas (RF-4, plan §3.2).

    Returns:
        Por salida, si TA-Lib la devuelve entera.

    Raises:
        TaLibExecutionError: SI TA-Lib rechaza la combinación (p. ej. ``TA_BAD_PARAM``).
    """
    n = rows if rows is not None else max(MIN_PROBE_ROWS, 2 * max(fn.lookback, 0))
    outputs = _run(fn, synthetic_inputs(fn.source_columns, n))
    return [bool(np.issubdtype(a.dtype, np.integer)) for a in outputs]


def global_config() -> dict[str, int]:
    """Configuración global de TA-Lib (solo lectura; RF-6): compatibilidad y *unstable period*
    de cada función con el *flag* de TA-Lib *unstable period*."""
    config = {"compatibility": int(talib.get_compatibility())}
    for name in talib.get_functions():
        if UNSTABLE_FLAG in talib.abstract.Function(name).function_flags:
            config[f"unstable_period.{name}"] = int(talib.get_unstable_period(name))
    return config


# --------------------------------------------------------------------------- salidas


def _null_before(series: pl.Series, lookback: int) -> pl.Series:
    """Nulos en las filas ``< lookback`` (calentamiento de salidas enteras)."""
    if lookback <= 0:
        return series
    keep = pl.int_range(pl.len()) >= lookback
    return series.to_frame().select(pl.when(keep).then(pl.col(series.name))).to_series()


def _persist_integer(fn: ResolvedFunction, column: str, values: np.ndarray[Any, Any]) -> pl.Series:
    """Patrón/categoría → ``Int16`` (nulo en el calentamiento; fuera de rango → error)."""
    tail = values[max(fn.lookback, 0) :]
    if tail.size:
        low, high = int(tail.min()), int(tail.max())
        if low < INT16_RANGE[0] or high > INT16_RANGE[1]:
            raise OutputOutOfRangeError(fn.series_id, column, low, high)
    series = _null_before(pl.Series(column, values, dtype=pl.Int64), fn.lookback)
    return series.cast(pl.Int16)


def _persist_index(
    fn: ResolvedFunction, column: str, values: np.ndarray[Any, Any], times: pl.Series
) -> pl.Series:
    """Índice de TA-Lib (posición en la subsecuencia) → ``open_time_ms`` de esa fila."""
    tail = values[max(fn.lookback, 0) :]
    if tail.size and (int(tail.min()) < 0 or int(tail.max()) >= times.len()):
        msg = f"{fn.series_id}.{column}: índices fuera de la serie calculada"
        raise TaLibExecutionError(msg)
    positions = _null_before(pl.Series(column, values, dtype=pl.Int64), fn.lookback)
    return times.gather(positions).alias(column)


def _persist(
    fn: ResolvedFunction,
    meta: OutputMeta,
    values: np.ndarray[Any, Any],
    times: pl.Series,
) -> pl.Series:
    column = fn.columns[meta.name]
    if meta.kind == "real":
        return _persist_real(column, values)
    if meta.kind == "index":
        return _persist_index(fn, column, values, times)
    return _persist_integer(fn, column, values)


# ------------------------------------------------------------------------- estabilidad


def _first_non_null(frame: pl.DataFrame, columns: Sequence[str]) -> dict[str, int | None]:
    if frame.height == 0:
        return dict.fromkeys(columns)
    row = frame.select(
        pl.col(TIME_COLUMN).filter(pl.col(c).is_not_null()).first().alias(c) for c in columns
    ).row(0)
    return {c: (int(v) if v is not None else None) for c, v in zip(columns, row, strict=True)}


def stability(fn: ResolvedFunction, frame: pl.DataFrame) -> Stability:
    """``Stability`` (RF-17): primera fila no nula, o ``unstable_period_unknown`` si la función
    tiene el *flag* de TA-Lib *unstable period*."""
    columns = list(fn.columns.values())
    if UNSTABLE_FLAG in fn.meta.flags:
        return Stability(
            stable_from_ms=None,
            by_column=dict.fromkeys(columns),
            rule=_UNSTABLE_RULE,
            reason="unstable_period_unknown",
        )
    by_column = _first_non_null(frame, columns)
    values = list(by_column.values())
    if any(v is None for v in values):
        return Stability(
            stable_from_ms=None, by_column=by_column, rule="first_non_null", reason="not_converged"
        )
    return Stability(
        stable_from_ms=max(v for v in values if v is not None),
        by_column=by_column,
        rule="first_non_null",
        reason="first_non_null",
    )


# ------------------------------------------------------------------------------ motor


def compute_function(src: pl.DataFrame, fn: ResolvedFunction) -> FunctionResult:
    """Calcula ``fn`` sobre la serie **completa** ``src`` (``open_time_ms`` + entradas).

    Warnings: ``insufficient_history`` (filas válidas ≤ *lookback*: columnas todas nulas) y
    ``null_inputs=N`` (filas con alguna entrada nula o no finita, que quedan nulas).

    Raises:
        ValueError: SI falta ``open_time_ms`` o alguna columna de entrada.
        OutputOutOfRangeError: SI una salida entera no cabe en ``Int16``.
        TaLibExecutionError: SI TA-Lib lanza una excepción.
    """
    started = time.perf_counter()
    inputs = fn.source_columns
    missing = [c for c in (TIME_COLUMN, *inputs) if c not in src.columns]
    if missing:
        msg = f"Columnas de entrada ausentes para {fn.series_id}: {missing}"
        raise ValueError(msg)

    valid = pl.all_horizontal(*(pl.col(c).is_not_null() & pl.col(c).is_finite() for c in inputs))
    sub = src.select(TIME_COLUMN, *inputs).filter(valid.fill_null(value=False))
    arrays = {c: _as_compute_array(sub[c]) for c in inputs}
    outputs = _run(fn, arrays) if sub.height else [np.empty(0)] * len(fn.meta.outputs)

    times = sub[TIME_COLUMN]
    columns = [
        _persist(fn, meta, values, times)
        for meta, values in zip(fn.meta.outputs, outputs, strict=True)
    ]
    computed = (
        pl.DataFrame([times, *columns])
        if sub.height
        else pl.DataFrame({TIME_COLUMN: pl.Series([], dtype=pl.Int64)}).with_columns(
            pl.lit(None, dtype=s.dtype).alias(s.name) for s in columns
        )
    )
    if sub.height == src.height:
        frame = computed
    else:
        frame = src.select(TIME_COLUMN).join(
            computed, on=TIME_COLUMN, how="left", maintain_order="left"
        )

    null_inputs = src.height - sub.height
    warnings: list[str] = []
    if sub.height <= fn.lookback:
        warnings.append("insufficient_history")
    if null_inputs:
        warnings.append(f"null_inputs={null_inputs}")
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.debug(
        "function_computed",
        function=fn.name,
        group=fn.meta.group,
        series_id=fn.series_id,
        rows=src.height,
        compute_duration_ms=round(elapsed_ms, 2),
    )
    return FunctionResult(
        resolved=fn,
        frame=frame,
        stability=stability(fn, frame),
        warnings=tuple(warnings),
        null_inputs=null_inputs,
        valid_rows=sub.height,
    )


__all__ = [
    "COMPUTE_DTYPE",
    "INT16_RANGE",
    "PERSIST_REAL",
    "TIME_COLUMN",
    "FunctionResult",
    "compute_function",
    "global_config",
    "integer_outputs",
    "probe",
    "stability",
    "synthetic_inputs",
]
