"""L2: estrategia integrada ``keltner_breakout`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame, ohlc_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "keltner_breakout"
PARAMS = {"n": 5, "k": 1.0}


def test_keltner_breakout_behavior() -> None:
    # n=5, k=1.0.
    # t=4: ema=10, atr=0, upper=10, lower=10. close=10 (no signal)
    # t=5: close=15. ema=11, atr=2.5, upper=13.5, lower=8.5. close > 13.5 -> LONG
    # t=6: close=10. ema=11.2, atr=2.5, upper=13.7, lower=8.7. close=10 (stay LONG)
    # t=7: close=5. ema=10.3, atr=5, upper=15.3, lower=5.3. close < 5.3 -> FLAT
    candles = ohlc_frame(
        high=[10.0] * 5 + [15.0, 10.0, 5.0],
        low=[10.0] * 5 + [10.0, 10.0, 5.0],
        close=[10.0] * 5 + [15.0, 10.0, 5.0],
    )
    # open = close by default in ohlc_frame
    pos = builtin_position(KIND, candles, **PARAMS)
    assert pos == [0, 0, 0, 0, 0, 1, 1, 0]


def test_warmup_is_n_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(**PARAMS)) == 5
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), **PARAMS)
    assert pos[:5] == [0, 0, 0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(KIND, candles_frame([5.0] * 8), **PARAMS)
    assert pos == [0] * 8


@pytest.mark.parametrize("params", [{"n": 1}, {"n": 10001}, {"k": 0.4}, {"k": 10.1}])
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
