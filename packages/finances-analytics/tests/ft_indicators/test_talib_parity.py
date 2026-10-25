"""T2.7: contraste directo con TA-Lib (spec RF-2 "Valores de referencia", plan §3.11).

TA-Lib solo se importa aquí (dependencia del extra ``[dev]``). Para cada indicador con
``IndicatorDef.talib``, sobre series sin nulos (paseo aleatorio 5.000 velas, serie plana,
precios extremos 1e-6 y 1e6):

1. Kernel ``Float64`` vs TA-Lib (``double``) con ``rtol=1e-9`` desde la primera fila no nula
   (mismo índice de calentamiento exigido) o desde el ajuste documentado en
   ``IndicatorDef.talib`` (``macd`` desde ``stable_from`` con ``atol=1e-6·escala``, ``stoch``
   desde ``k+smooth_k+d−3``, ``roc`` sin filas de base 0).
2. Persistido ``Float32`` vs TA-Lib dentro de la tabla §3.9 desde ``stable_from_ms``
   (``obv``: semilla que no decae ⇒ desde la primera fila).
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import numpy.typing as npt
import polars as pl
import pytest
import talib
from finances_core.schemas.indicators import IndicatorSpec
from talib import abstract

from finances_analytics.ft_indicators import engine
from finances_analytics.ft_indicators.catalog import CATALOG, ResolvedIndicator, resolve
from finances_analytics.ft_indicators.convergence import stable_index
from finances_analytics.ft_indicators.precision import scale, tolerance

from .helpers import flat_series, ohlcv_frame, random_walk

FloatArray = npt.NDArray[np.float64]

SERIES: dict[str, pl.DataFrame] = {
    "random_walk": ohlcv_frame(random_walk(5_000, seed=3)),
    "flat": ohlcv_frame(flat_series(300)),
    "extreme_low": ohlcv_frame(random_walk(2_000, seed=4, scale=1e-6)),
    "extreme_high": ohlcv_frame(random_walk(2_000, seed=5, scale=1e6)),
}
TALIB_NAMES = [name for name, d in CATALOG.items() if d.talib is not None]


def _resolved(name: str) -> ResolvedIndicator:
    return resolve(IndicatorSpec.model_validate({"name": name}))


def _talib_param(expr: str, params: Mapping[str, object]) -> object:
    if expr in params:
        value = params[expr]
        return float(value) if isinstance(value, float) else value
    return int(expr)


def talib_outputs(resolved: ResolvedIndicator, frame: pl.DataFrame) -> dict[str, FloatArray]:
    """Salidas TA-Lib por sufijo propio, a partir de los metadatos de ``IndicatorDef.talib``."""
    equivalent = resolved.definition.talib
    assert equivalent is not None
    inputs = [frame[c].cast(pl.Float64).to_numpy() for c in resolved.inputs]
    kwargs = {k: _talib_param(v, resolved.params) for k, v in equivalent.params.items()}
    out: dict[str, FloatArray] = {}
    for suffix, target in equivalent.outputs.items():
        function, output = target.split(":")
        result = getattr(talib, function)(*inputs, **kwargs)
        names = abstract.Function(function).output_names
        arrays = result if isinstance(result, tuple) else (result,)
        out[suffix] = np.asarray(arrays[names.index(output)], dtype=np.float64)
    return out


def _start_row(resolved: ResolvedIndicator, column: str, ours: FloatArray) -> int:
    equivalent = resolved.definition.talib
    assert equivalent is not None
    compare_from = equivalent.compare_from(resolved.params)
    if compare_from == "first_non_null":
        return int(np.argmax(~np.isnan(ours)))
    if compare_from == "stable_from":
        return stable_index(resolved.stages[column])
    assert isinstance(compare_from, int)
    return compare_from


def _excluded(resolved: ResolvedIndicator, frame: pl.DataFrame) -> npt.NDArray[np.bool_]:
    if resolved.definition.name != "roc":
        return np.zeros(frame.height, dtype=bool)
    source = frame[resolved.inputs[0]].cast(pl.Float64).to_numpy()
    period = int(resolved.params["period"])
    base = np.concatenate([np.full(period, np.nan), source[:-period]])
    return base == 0


def test_every_talib_equivalent_has_a_parity_case() -> None:
    covered = {n for n in TALIB_NAMES if n in PARITY_CASES}
    assert covered == {n for n, d in CATALOG.items() if d.talib is not None}
    assert len(covered) == 11
    assert CATALOG["vwap"].talib is None


def _parity_float64(name: str, series: str) -> dict[str, tuple[float, float]]:
    """Compara y devuelve el error máximo (abs, rel) por columna."""
    frame = SERIES[series]
    resolved = _resolved(name)
    ours_frame = engine.compute_float64(frame, [resolved]).fill_null(np.nan)
    theirs = talib_outputs(resolved, frame)
    excluded = _excluded(resolved, frame)
    equivalent = resolved.definition.talib
    assert equivalent is not None
    errors: dict[str, tuple[float, float]] = {}
    for suffix, column in resolved.columns.items():
        ours = ours_frame[column].to_numpy()
        ref = theirs[suffix]
        first_ours = int(np.argmax(~np.isnan(ours)))
        first_ref = int(np.argmax(~np.isnan(ref)))
        start = _start_row(resolved, column, ours)
        if equivalent.compare_from(resolved.params) == "first_non_null":
            # Convención idéntica: mismo índice de calentamiento exigido.
            assert first_ours == first_ref, f"{column}: calentamiento {first_ours} != {first_ref}"
        mask = np.arange(frame.height) >= start
        mask &= ~excluded
        a, b = ours[mask], ref[mask]
        atol = 0.0
        if name == "macd":
            atol = 1e-6 * scale(resolved, frame)
        np.testing.assert_allclose(a, b, rtol=1e-9, atol=atol, err_msg=column)
        diff = np.abs(a - b)
        rel = diff / np.where(b == 0, 1.0, np.abs(b))
        errors[column] = (float(diff.max(initial=0.0)), float(rel.max(initial=0.0)))
    return errors


PARITY_CASES = {
    name: [
        pytest.param(
            name,
            series,
            id=f"{name}-{series}",
        )
        for series in SERIES
    ]
    for name in TALIB_NAMES
}


@pytest.mark.parametrize(("name", "series"), [p for cases in PARITY_CASES.values() for p in cases])
def test_kernel_float64_matches_talib(name: str, series: str) -> None:
    _parity_float64(name, series)


@pytest.mark.parametrize("series", list(SERIES))
@pytest.mark.parametrize("name", TALIB_NAMES)
def test_persisted_float32_within_tolerance_table(name: str, series: str) -> None:
    frame = SERIES[series]
    resolved = _resolved(name)
    result = engine.compute(frame, [resolved])
    theirs = talib_outputs(resolved, frame)
    stab = result.outputs[resolved.indicator_id].stability
    times = frame["open_time_ms"].to_list()
    excluded = _excluded(resolved, frame)
    ref_scale = scale(resolved, frame)
    for suffix, column in resolved.columns.items():
        stable_ms = stab.by_column[column]
        start = times.index(stable_ms) if stable_ms is not None else 0
        ours = result.frame[column].cast(pl.Float64).fill_null(np.nan).to_numpy()
        ref = theirs[suffix]
        mask = (np.arange(frame.height) >= start) & ~excluded & ~np.isnan(ref)
        if name == "stoch":
            mask &= np.arange(frame.height) >= _start_row(resolved, column, ours)
        tol = tolerance(name, suffix)
        allowed = tol.atol + tol.atol_scaled * ref_scale + tol.rtol * np.abs(ref[mask])
        diff = np.abs(ours[mask] - ref[mask])
        assert not np.isnan(diff).any(), column
        assert (diff <= allowed).all(), f"{column}: exceso {float((diff - allowed).max())}"
