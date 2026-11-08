"""L2: estrategia integrada ``rsi2_connors`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "rsi2_connors"
PARAMS = {"period": 2, "lower": 20.0, "upper": 50.0}


def test_rsi2_connors_behavior() -> None:
    # period=2, lower=20, upper=50. rsi_wilder(period=2) ya está probado en
    # test_strategies_lib.py; aquí se verifica el comportamiento de la estrategia (RSI
    # real: [_, _, 50, 0, 75, 83.33, 35.71]).
    # t=3: RSI=0 < 20 -> entra largo. t=4: RSI=75 > 50 -> sale a plano. t=5: RSI=83.33
    # > 50 (ya plano, sin cambio). t=6: RSI=35.71 (entre 20 y 50: mantiene plano).
    pos = builtin_position(KIND, candles_frame([10.0, 10.0, 10.0, 8.0, 11.0, 12.0, 10.0]), **PARAMS)
    assert pos == [0, 0, 0, 1, 0, 0, 0]


def test_warmup_is_period_plus_one_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(**PARAMS)) == 3
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), **PARAMS)
    assert pos[:3] == [0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    # RSI=50 for flat series. 50 is not < 20.
    pos = builtin_position(KIND, candles_frame([5.0] * 8), **PARAMS)
    assert pos == [0] * 8


@pytest.mark.parametrize(
    "params", [{"period": 0}, {"period": 1001}, {"period": 2, "lower": 60.0, "upper": 40.0}]
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
