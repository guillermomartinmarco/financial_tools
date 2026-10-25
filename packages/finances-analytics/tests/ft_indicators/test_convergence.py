"""T2.8: ``convergence`` — ``k_star``, ``stable_index``, ``stability`` (RF-16)."""

from __future__ import annotations

import math

import pytest
from finances_core.schemas.indicators import IndicatorSpec
from hypothesis import given
from hypothesis import strategies as st

from finances_analytics.ft_indicators.catalog import resolve
from finances_analytics.ft_indicators.convergence import EPSILON, k_star, stability, stable_index

TIMES = [1_000 * i for i in range(1_000)]


def _default(name: str) -> dict[str, int | None]:
    resolved = resolve(IndicatorSpec.model_validate({"name": name}))
    return {col: stable_index(stage) for col, stage in resolved.stages.items()}


def test_epsilon_is_versioned_constant() -> None:
    assert EPSILON == 1e-6


def test_k_star_reference_values() -> None:
    assert k_star(2 / 21) == 139
    assert k_star(1 / 14) == 187
    assert k_star(2 / 13) == 83
    assert k_star(2 / 27) == 180
    assert k_star(0.2) == 62


def test_default_stable_indices() -> None:
    assert _default("ema") == {"ema_20": 158}
    assert _default("rsi") == {"rsi_14": 201}
    assert _default("atr") == {"atr_14": 201}
    assert _default("adx") == {"adx_14": 387, "adx_14_plus_di": 200, "adx_14_minus_di": 200}
    assert _default("macd") == {
        "macd_12_26_9_line": 205,
        "macd_12_26_9_signal": 267,
        "macd_12_26_9_hist": 267,
    }


@given(st.floats(min_value=1e-4, max_value=0.999))
def test_k_star_is_minimal(alpha: float) -> None:
    k = k_star(alpha)
    assert (1 - alpha) ** k < EPSILON
    assert k == 1 or (1 - alpha) ** (k - 1) >= EPSILON


def test_recursive_stability_maps_index_to_open_time() -> None:
    resolved = resolve(IndicatorSpec(name="ema"))
    stab = stability(resolved, TIMES, {"ema_20": TIMES[19]})
    assert stab.by_column == {"ema_20": TIMES[158]}
    assert stab.stable_from_ms == TIMES[158]
    assert stab.reason == "converged"
    assert "alpha=2/21" in stab.rule and "k=139" in stab.rule


def test_series_stable_from_is_max_of_columns() -> None:
    resolved = resolve(IndicatorSpec(name="macd"))
    stab = stability(resolved, TIMES, {})
    assert stab.by_column["macd_12_26_9_line"] == TIMES[205]
    assert stab.stable_from_ms == TIMES[267]


def test_null_rows_shift_the_stable_index() -> None:
    """Los índices cuentan solo filas válidas de la entrada."""
    resolved = resolve(IndicatorSpec(name="rsi"))
    valid_times = [t for i, t in enumerate(TIMES) if i not in {10, 50, 300}]
    stab = stability(resolved, valid_times, {})
    assert stab.stable_from_ms == TIMES[203]


def test_short_series_is_not_converged() -> None:
    resolved = resolve(IndicatorSpec(name="adx"))
    stab = stability(resolved, TIMES[:300], {})
    assert stab.by_column["adx_14_plus_di"] == TIMES[200]
    assert stab.by_column["adx_14"] is None
    assert stab.stable_from_ms is None
    assert stab.reason == "not_converged"


def test_obv_is_non_decaying() -> None:
    resolved = resolve(IndicatorSpec(name="obv"))
    stab = stability(resolved, TIMES, {"obv": TIMES[0]})
    assert stab.stable_from_ms is None
    assert stab.by_column == {"obv": None}
    assert stab.reason == "non_decaying_seed"


@pytest.mark.parametrize("name", ["sma", "wma", "bb", "stoch", "roc", "vwap"])
def test_non_recursive_use_first_non_null(name: str) -> None:
    resolved = resolve(IndicatorSpec.model_validate({"name": name}))
    first = {col: TIMES[40 + i] for i, col in enumerate(resolved.columns.values())}
    stab = stability(resolved, TIMES, first)
    assert stab.by_column == first
    assert stab.stable_from_ms == max(first.values())
    assert stab.reason == "first_non_null"


def test_non_recursive_without_values_is_not_converged() -> None:
    resolved = resolve(IndicatorSpec(name="sma"))
    stab = stability(resolved, TIMES[:5], {"sma_20": None})
    assert stab.stable_from_ms is None
    assert stab.reason == "not_converged"


def test_stable_index_is_analytic_max_of_inputs() -> None:
    resolved = resolve(IndicatorSpec(name="macd", params={"fast": 3, "slow": 5, "signal": 2}))
    idx = {c: stable_index(s) for c, s in resolved.stages.items()}
    slow = 4 + k_star(2 / 6)
    assert idx["macd_3_5_2_line"] == max(2 + k_star(2 / 4), slow)
    assert idx["macd_3_5_2_signal"] == max(5, slow) + k_star(2 / 3)
    assert math.isfinite(idx["macd_3_5_2_hist"] or math.inf)
