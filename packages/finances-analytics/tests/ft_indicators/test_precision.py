"""T2.9: tabla de tolerancias ``f32`` (plan §3.9) publicada en ``catalog()`` y alcanzable."""

from __future__ import annotations

import polars as pl
import pytest
from finances_core.schemas.indicators import IndicatorSpec

from finances_analytics.ft_indicators.catalog import CATALOG, ResolvedIndicator, catalog, resolve
from finances_analytics.ft_indicators.convergence import stable_index
from finances_analytics.ft_indicators.kernels.common import evaluate
from finances_analytics.ft_indicators.precision import TOLERANCES, output_key, scale, tolerance

from .helpers import ohlcv_frame, random_walk

PLAN_TABLE = {
    ("sma", "sma"): (1e-5, 0.0, 0.0),
    ("wma", "wma"): (1e-5, 0.0, 0.0),
    ("vwap", "vwap"): (1e-5, 0.0, 0.0),
    ("bb", "middle"): (1e-5, 0.0, 0.0),
    ("ema", "ema"): (1e-5, 0.0, 0.0),
    ("atr", "atr"): (1e-5, 0.0, 0.0),
    ("bb", "upper"): (1e-5, 0.0, 1e-5),
    ("bb", "lower"): (1e-5, 0.0, 1e-5),
    ("macd", "line"): (0.0, 0.0, 1e-5),
    ("macd", "signal"): (0.0, 0.0, 1e-5),
    ("macd", "hist"): (0.0, 0.0, 1e-5),
    ("rsi", "rsi"): (0.0, 1e-3, 0.0),
    ("stoch", "k"): (0.0, 1e-3, 0.0),
    ("stoch", "d"): (0.0, 1e-3, 0.0),
    ("adx", "adx"): (0.0, 1e-2, 0.0),
    ("adx", "plus_di"): (0.0, 1e-2, 0.0),
    ("adx", "minus_di"): (0.0, 1e-2, 0.0),
    ("roc", "roc"): (0.0, 1e-4, 0.0),
    ("obv", "obv"): (0.0, 0.0, 1e-6),
}


def test_every_catalog_column_has_a_tolerance_matching_the_plan() -> None:
    seen = set()
    for name, defn in CATALOG.items():
        for suffix in defn.outputs:
            key = output_key(name, suffix)
            tol = tolerance(name, suffix)
            assert (tol.rtol, tol.atol, tol.atol_scaled) == PLAN_TABLE[(name, key)]
            seen.add((name, key))
    assert seen == set(PLAN_TABLE)
    assert set(TOLERANCES) == set(CATALOG)


def test_catalog_publishes_stable_from_tolerance_and_talib() -> None:
    for entry in catalog():
        defn = CATALOG[entry.name]
        assert entry.stable_from
        assert set(entry.f32_tolerance) == {output_key(entry.name, s) for s in defn.outputs}
        assert entry.f32_tolerance[output_key(entry.name, defn.outputs[0])]["rtol"] >= 0
        assert (entry.talib_equivalent is None) == (entry.name == "vwap")
    by_name = {e.name: e for e in catalog()}
    assert "158" in by_name["ema"].stable_from
    assert "non_decaying_seed" in by_name["obv"].stable_from
    assert by_name["macd"].talib_equivalent is not None
    assert by_name["macd"].talib_equivalent.compare_from == "stable_from"
    assert by_name["stoch"].talib_equivalent is not None
    assert by_name["stoch"].talib_equivalent.compare_from == 17
    assert by_name["roc"].talib_equivalent is not None
    assert by_name["roc"].talib_equivalent.excluded_rows


def _first_compared_row(resolved: ResolvedIndicator, column: str, f64: pl.Series) -> int:
    stage = resolved.stages.get(column)
    if stage is not None:
        return stable_index(stage)  # serie sin nulos: índice válido == fila
    non_null = f64.is_not_null().arg_true()
    return int(non_null[0])


@pytest.mark.parametrize("name", list(CATALOG))
def test_float32_end_to_end_is_within_tolerance_from_stable_from(name: str) -> None:
    """La tabla es alcanzable: el mismo kernel en ``Float32`` de punta a punta cae dentro."""
    frame = ohlcv_frame(random_walk(5_000, seed=23))
    resolved = resolve(IndicatorSpec.model_validate({"name": name}))
    kernel = resolved.definition.kernel
    names = {(s or "value"): c for s, c in resolved.columns.items()}
    f64 = evaluate(frame, kernel, resolved.params).rename(names)
    f32 = evaluate(frame, kernel, resolved.params, pl.Float32).rename(names)
    ref_scale = scale(resolved, frame)
    for suffix, column in resolved.columns.items():
        assert f32.schema[column] == pl.Float32, column
        assert f32[column].is_null().to_list() == f64[column].is_null().to_list(), column
        start = _first_compared_row(resolved, column, f64[column])
        tol = tolerance(name, suffix)
        ref = f64[column][start:].to_list()
        got = f32[column][start:].to_list()
        worst = max(
            abs(g - r) - tol.allowed(r, ref_scale)
            for g, r in zip(got, ref, strict=True)
            if r is not None and g is not None
        )
        assert worst <= 0.0, f"{column}: excede la tolerancia en {worst}"
