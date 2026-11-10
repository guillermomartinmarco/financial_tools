"""L5: estrategia integrada ``channel_breakout`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, ohlc_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "channel_breakout"
HIGH = [10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 10.0]
LOW = [9.0, 10.0, 11.0, 12.0, 13.0, 12.0, 11.0, 10.0, 9.0]
CLOSE = [10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 10.0]


def test_entry_resistance_and_exit_support_are_independent_windows() -> None:
    pos = builtin_position(
        KIND, ohlc_frame(high=HIGH, low=LOW, close=CLOSE), entry_window=3, exit_window=2
    )
    assert pos == [0, 0, 0, 1, 1, 1, 1, 1, 1]


def test_entry_window_and_exit_window_act_independently() -> None:
    # Misma resistencia de entrada (entry_window=3) con ventanas de salida distintas:
    # warmup = max(entry_window, exit_window) difiere, y por eso el momento de entrada y
    # la decisión final cambian solo por exit_window (ventanas verdaderamente independientes).
    high = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 15.5, 9.0]
    low = [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 14.5, 8.5]
    close = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 15.0, 9.0]
    candles = ohlc_frame(high=high, low=low, close=close)
    pos_loose = builtin_position(KIND, candles, entry_window=3, exit_window=5)
    pos_tight = builtin_position(KIND, candles, entry_window=3, exit_window=2)
    assert pos_loose == [0, 0, 0, 0, 0, 1, 1, 1, 0]
    assert pos_tight == [0, 0, 0, 1, 1, 1, 1, 1, 0]
    assert pos_loose != pos_tight


def test_warmup_is_the_larger_window_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(entry_window=20, exit_window=10)) == 20
    pos = builtin_position(
        KIND, ohlc_frame(high=HIGH, low=LOW, close=CLOSE), entry_window=5, exit_window=3
    )
    assert pos[:5] == [0, 0, 0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(
        KIND,
        ohlc_frame(high=[5.0] * 8, low=[5.0] * 8, close=[5.0] * 8),
        entry_window=3,
        exit_window=2,
    )
    assert pos == [0] * 8


@pytest.mark.parametrize(
    "params", [{"entry_window": 1}, {"exit_window": 1}, {"entry_window": 10_001}]
)
def test_invalid_params(params: dict[str, int]) -> None:
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
    assert (
        strategy.Params.model_validate({"entry_window": 3, "exit_window": 2}).allow_short is False
    )


def test_breakdown_of_support_opens_short_when_allow_short() -> None:
    pos = builtin_position(
        KIND,
        ohlc_frame(
            high=[10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 5.0, 5.0],
            low=[9.0, 10.0, 11.0, 12.0, 13.0, 12.0, 11.0, 10.0, 4.0, 4.0],
            close=[10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 4.5, 4.5],
        ),
        entry_window=3,
        exit_window=2,
        allow_short=True,
    )
    assert pos == [0, 0, 0, 1, 1, 1, 1, 1, 0, -1]
