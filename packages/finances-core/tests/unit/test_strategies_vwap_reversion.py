"""L5: estrategia integrada ``vwap_reversion`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, ohlcv_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "vwap_reversion"
PARAMS = {"n": 2, "threshold": 0.02}


def test_drop_below_vwap_threshold_enters_long_and_recovery_exits() -> None:
    pos = builtin_position(
        KIND,
        ohlcv_frame(
            high=[10.0, 11.0, 10.0, 11.0, 12.0],
            low=[9.0, 10.0, 9.0, 10.0, 11.0],
            close=[10.0, 11.0, 9.0, 11.0, 12.0],
            volume=[100.0] * 5,
        ),
        **PARAMS,
    )
    assert pos == [0, 0, 1, 0, 0]


def test_warmup_is_n_minus_one_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(n=20)) == 19
    pos = builtin_position(
        KIND,
        ohlcv_frame(high=[1.0, 2.0], low=[1.0, 2.0], close=[1.0, 2.0], volume=[100.0, 100.0]),
        **PARAMS,
    )
    assert pos == [0, 0]


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(
        KIND,
        ohlcv_frame(high=[5.0] * 8, low=[5.0] * 8, close=[5.0] * 8, volume=[100.0] * 8),
        **PARAMS,
    )
    assert pos == [0] * 8


@pytest.mark.parametrize("params", [{"n": 1}, {"threshold": -0.1}, {"threshold": 1.1}])
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
