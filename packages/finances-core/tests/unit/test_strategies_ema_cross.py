"""L2: estrategia integrada ``ema_cross`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "ema_cross"


def test_cross_up_on_trend_change_holds_long() -> None:
    # fast=2, slow=3: EMA_2 cruza a EMA_3 por encima al saltar de 10 a 20 en t=3 y se
    # mantiene largo mientras dure la tendencia alcista (sin cruce a la baja).
    pos = builtin_position(
        KIND, candles_frame([10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 20.0]), fast=2, slow=3
    )
    assert pos == [0, 0, 0, 1, 1, 1, 1]


def test_warmup_is_slow_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(fast=2, slow=3)) == 3
    pos = builtin_position(KIND, candles_frame([10.0, 20.0, 30.0, 30.0, 30.0]), fast=2, slow=3)
    assert pos[:3] == [0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(KIND, candles_frame([5.0] * 8), fast=2, slow=3)
    assert pos == [0] * 8


@pytest.mark.parametrize(("fast", "slow"), [(5, 5), (6, 5), (1, 3), (2, 10_001)])
def test_invalid_params(fast: int, slow: int) -> None:
    registry = get_registry(FinancesSettings())
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": {"fast": fast, "slow": slow}}, registry)
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
    assert strategy.Params.model_validate({"fast": 2, "slow": 3}).allow_short is False


def test_bearish_cross_opens_short_through_one_flat_bar_when_allow_short() -> None:
    # fast=2, slow=3: cruce bajista en t=6 (20->8) abre corto en t=7 (una barra en plano
    # en t=6, Enmienda G2b); cruce alcista en t=9 (8->20) vuelve a plano.
    pos = builtin_position(
        KIND,
        candles_frame([10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 8.0, 8.0, 8.0, 20.0]),
        fast=2,
        slow=3,
        allow_short=True,
    )
    assert pos == [0, 0, 0, 1, 1, 1, 0, -1, -1, 0]
