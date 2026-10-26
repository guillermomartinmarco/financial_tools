"""T5.4 (ft_datasets) — ``ft_indicators`` sobre datos reales verificados de ``benchmark-v1``.

BTCUSDT ``1m`` (Q1 2024, ~131 k velas) y AAPL ``1d`` (2015-2024): el resultado persistido
``Float32`` de cada indicador con equivalente TA-Lib queda dentro de la tabla de
tolerancias de ``ft_indicators`` (plan §3.9) desde ``stable_from_ms``, igual que
``tests/ft_indicators/test_talib_parity.py`` con series sintéticas. Marcador ``real_data``.
"""

from __future__ import annotations

from functools import cache

import numpy as np
import numpy.typing as npt
import polars as pl
import pytest
import talib
from finances_core.datasets.testing import require_dataset
from finances_core.schemas.indicators import IndicatorSpec
from talib import abstract

from finances_analytics.ft_indicators import engine
from finances_analytics.ft_indicators.catalog import CATALOG, ResolvedIndicator, resolve
from finances_analytics.ft_indicators.convergence import stable_index
from finances_analytics.ft_indicators.precision import scale, tolerance

pytestmark = pytest.mark.real_data

RECIPE = "benchmark-v1"
ITEMS = {
    "btcusdt_1m": "binance/spot/BTCUSDT/1m@2024-01-01..2024-04-01",
    "aapl_1d": "yfinance/equity/AAPL/1d@2015-01-01..2025-01-01",
}
TALIB_NAMES = [name for name, d in CATALOG.items() if d.talib is not None]
FloatArray = npt.NDArray[np.float64]


@cache
def _frame(series: str) -> pl.DataFrame:
    return (
        require_dataset(RECIPE, ITEMS[series])
        .select("open_time_ms", "open", "high", "low", "close", "volume")
        .collect()
    )


def _talib_outputs(resolved: ResolvedIndicator, frame: pl.DataFrame) -> dict[str, FloatArray]:
    equivalent = resolved.definition.talib
    assert equivalent is not None
    inputs = [frame[c].cast(pl.Float64).to_numpy() for c in resolved.inputs]
    kwargs: dict[str, object] = {}
    for key, expr in equivalent.params.items():
        value = resolved.params[expr] if expr in resolved.params else int(expr)
        kwargs[key] = float(value) if isinstance(value, float) else value
    out: dict[str, FloatArray] = {}
    for suffix, target in equivalent.outputs.items():
        function, output = target.split(":")
        result = getattr(talib, function)(*inputs, **kwargs)
        arrays = result if isinstance(result, tuple) else (result,)
        names = abstract.Function(function).output_names
        out[suffix] = np.asarray(arrays[names.index(output)], dtype=np.float64)
    return out


def _excluded(resolved: ResolvedIndicator, frame: pl.DataFrame) -> npt.NDArray[np.bool_]:
    if resolved.definition.name != "roc":
        return np.zeros(frame.height, dtype=bool)
    source = frame[resolved.inputs[0]].cast(pl.Float64).to_numpy()
    period = int(resolved.params["period"])
    base = np.concatenate([np.full(period, np.nan), source[:-period]])
    return np.asarray(base == 0, dtype=bool)


def _stoch_start(resolved: ResolvedIndicator, column: str) -> int:
    equivalent = resolved.definition.talib
    assert equivalent is not None
    compare_from = equivalent.compare_from(resolved.params)
    if compare_from == "stable_from":
        return stable_index(resolved.stages[column])
    return compare_from if isinstance(compare_from, int) else 0


@pytest.mark.parametrize("series", list(ITEMS))
@pytest.mark.parametrize("name", TALIB_NAMES)
def test_real_data_float32_within_talib_tolerance(name: str, series: str) -> None:
    frame = _frame(series)
    resolved = resolve(IndicatorSpec.model_validate({"name": name}))
    result = engine.compute(frame, [resolved])
    theirs = _talib_outputs(resolved, frame)
    stability = result.outputs[resolved.indicator_id].stability
    times = frame["open_time_ms"].to_list()
    excluded = _excluded(resolved, frame)
    ref_scale = scale(resolved, frame)
    for suffix, column in resolved.columns.items():
        stable_ms = stability.by_column[column]
        start = times.index(stable_ms) if stable_ms is not None else 0
        ours = result.frame[column].cast(pl.Float64).fill_null(np.nan).to_numpy()
        ref = theirs[suffix]
        mask = (np.arange(frame.height) >= start) & ~excluded & ~np.isnan(ref)
        if name == "stoch":
            mask &= np.arange(frame.height) >= _stoch_start(resolved, column)
        assert mask.any(), column
        tol = tolerance(name, suffix)
        allowed = tol.atol + tol.atol_scaled * ref_scale + tol.rtol * np.abs(ref[mask])
        diff = np.abs(ours[mask] - ref[mask])
        assert not np.isnan(diff).any(), column
        assert (diff <= allowed).all(), f"{series}/{column}: exceso {float((diff - allowed).max())}"
