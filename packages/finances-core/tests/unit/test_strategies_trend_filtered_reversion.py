"""L2: estrategia integrada ``trend_filtered_reversion`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "trend_filtered_reversion"
PARAMS = {"trend_n": 5, "rsi_period": 2, "rsi_lower": 40.0, "rsi_upper": 70.0}


def test_trend_filtered_reversion_behavior() -> None:
    # trend_n=5, rsi_period=2, lower=40, upper=70.
    # t=7: close=60, sma=52.0. close > sma (True). RSI=28.57 < 40 (True). -> LONG
    # t=8: close=100, sma=72.0. close > sma (True). RSI=71.4 > 70 (True). -> FLAT
    pos = builtin_position(KIND, candles_frame([10.0] * 5 + [100.0, 100.0, 60.0, 100.0]), **PARAMS)
    assert pos == [0, 0, 0, 0, 0, 0, 0, 1, 0]


def test_trend_filter_gates_trigger() -> None:
    # Same as above but with a downtrend.
    # [100, 100, 100, 100, 100, 50, 50, 20, 50]
    # t=7: close=20, sma=64.0. close < sma -> FLAT (even if RSI is low).
    pos = builtin_position(KIND, candles_frame([100.0] * 5 + [50.0, 50.0, 20.0, 50.0]), **PARAMS)
    assert pos == [0] * 9


def test_warmup_is_max_trend_rsi_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    # max(5-1, 2) = 4.
    assert strategy.warmup(strategy.Params(**PARAMS)) == 4
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), **PARAMS)
    assert pos[:4] == [0, 0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(KIND, candles_frame([5.0] * 8), **PARAMS)
    assert pos == [0] * 8


@pytest.mark.parametrize(
    "params", [{"trend_n": 1}, {"rsi_period": 0}, {"rsi_lower": 80.0, "rsi_upper": 70.0}]
)
def test_invalid_params(params: dict[str, float]) -> None:
    registry = get_registry(FinancesSettings())
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": params}, registry)
    assert info.value.code == "invalid_params"


def test_registered_as_builtin_with_defaults() -> None:
    registry = get_registry(FinancesSettings())
    assert registry.origins[KIND] == "builtin"
    strategy = registry.get(KIND)
    assert strategy.Params.model_validate(dict(strategy.default_params))


def test_readme_sections_diagram_and_table() -> None:
    folder = BUILTIN_DIR / KIND
    check_structure(folder)
    strategy = get_registry(FinancesSettings()).get(KIND)
    check_params_table(folder, strategy.Params, strategy.default_params)


def test_assert_strategy_contract() -> None:
    assert_strategy_contract(KIND)


# ------------------------------------------------- Enmienda G3: allow_short=True
def test_allow_short_false_by_default_is_byte_identical_to_pre_g3() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.Params.model_validate(PARAMS).allow_short is False


def test_downtrend_bounce_opens_short_when_allow_short() -> None:
    # Ca\u00edda sostenida (trend_n=5) seguida de un rebote que RSI(2) marca fuerte pero
    # que sigue por debajo de la media (a\u00fan descendiente) -> abre corto.
    closes = [200.0, 200.0, 200.0, 200.0, 100.0, 100.0, 130.0, 150.0]
    pos = builtin_position(
        KIND,
        candles_frame(closes),
        trend_n=5,
        rsi_period=2,
        rsi_lower=46.0,
        rsi_upper=90.0,
        allow_short=True,
    )
    assert pos == [0, 0, 0, 0, 0, 0, -1, 0]
