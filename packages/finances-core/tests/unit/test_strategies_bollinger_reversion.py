"""L2: estrategia integrada ``bollinger_reversion`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "bollinger_reversion"
PARAMS = {"n": 3, "k": 1.0}


def test_reversion_to_mean_behavior() -> None:
    # n=3, k=1.0.
    # closes: [10, 10, 10, 8, 11, 12, 10]
    # t=2: sma=10, std=0, lower=10. pos=0
    # t=3: close=8, sma=9.33, std=0.94, lower=9.33-0.94=8.39. 8 < 8.39 -> LONG
    # t=4: close=11, sma=9.66, std=1.24, lower=8.42. 11 > 9.66 -> FLAT
    # t=5: close=12, sma=(8+11+12)/3=10.33, std=std([8,11,12])=1.69. lower=8.64. 12 > 10.33 -> FLAT
    # t=6: close=10, sma=11.0, std=0.81, lower=10.19. 10 < 10.19 -> LONG
    pos = builtin_position(KIND, candles_frame([10.0, 10.0, 10.0, 8.0, 11.0, 12.0, 10.0]), **PARAMS)
    assert pos == [0, 0, 0, 1, 0, 0, 1]


def test_warmup_is_n_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(**PARAMS)) == 3
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), **PARAMS)
    assert pos[:3] == [0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    # std=0, close=middle, so close < lower is false.
    pos = builtin_position(KIND, candles_frame([5.0] * 8), **PARAMS)
    assert pos == [0] * 8


@pytest.mark.parametrize("params", [{"n": 1}, {"n": 10_001}, {"n": 3, "k": 0.1}])
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
