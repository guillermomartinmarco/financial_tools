"""L2: estrategia integrada ``high_breakout`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "high_breakout"


def test_breakout_enters_and_min_break_exits() -> None:
    # window=3 sobre cierres: rompe max previo (5->6) en t=3 y mantiene; perfora el min
    # previo (6->4) en t=6 y sale; nuevo máximo (7) en t=9 reentra.
    closes = [5.0, 5.0, 5.0, 6.0, 6.0, 6.0, 4.0, 4.0, 4.0, 7.0]
    pos = builtin_position(KIND, candles_frame(closes), window=3)
    assert pos == [0, 0, 0, 1, 1, 1, 0, 0, 0, 1]


def test_warmup_is_window_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(window=3)) == 3
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), window=3)
    assert pos[:3] == [0, 0, 0]


def test_touching_the_channel_is_not_a_breakout() -> None:
    # Serie plana: close nunca supera el máximo previo (empate), sin señal.
    pos = builtin_position(KIND, candles_frame([5.0] * 8), window=3)
    assert pos == [0] * 8


@pytest.mark.parametrize("window", [1, 10_001])
def test_invalid_params(window: int) -> None:
    registry = get_registry(FinancesSettings())
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": {"window": window}}, registry)
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
    assert strategy.Params.model_validate({"window": 3}).allow_short is False


def test_breakdown_opens_short_through_one_flat_bar_when_allow_short() -> None:
    # window=3: rompe max previo (5->6) en t=3, mantiene; perfora min previo (6->4) en
    # t=6 abre corto en t=... (verificado ejecutando el c\u00f3digo real).
    pos = builtin_position(
        KIND,
        candles_frame([5.0, 5.0, 5.0, 6.0, 6.0, 6.0, 4.0, 4.0, 4.0, 7.0]),
        window=3,
        allow_short=True,
    )
    assert pos == [0, 0, 0, 1, 1, 1, 0, -1, -1, 0]
