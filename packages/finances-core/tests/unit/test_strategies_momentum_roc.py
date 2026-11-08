"""L2: estrategia integrada ``momentum_roc`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "momentum_roc"
PARAMS = {"n": 3, "threshold": 0.0}


def test_long_while_momentum_positive_flat_when_negative() -> None:
    # roc(close,3): t4=11/10-1=+.1, t5=+.2, t6=+.3 (largo); t7=9/11-1<0 -> plano.
    closes = [10.0, 10.0, 10.0, 10.0, 11.0, 12.0, 13.0, 9.0, 8.0, 7.0]
    pos = builtin_position(KIND, candles_frame(closes), **PARAMS)
    assert pos == [0, 0, 0, 0, 1, 1, 1, 0, 0, 0]


def test_warmup_is_n_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(**PARAMS)) == 3
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), **PARAMS)
    assert pos[:3] == [0, 0, 0]


def test_threshold_is_strict_tie_holds_position() -> None:
    # Con threshold=0 y una serie plana, roc=0 nunca supera el umbral: queda plano.
    pos = builtin_position(KIND, candles_frame([5.0] * 8), **PARAMS)
    assert pos == [0] * 8


@pytest.mark.parametrize("params", [{"n": 0}, {"n": 10_001}, {"n": 3, "threshold": 11.0}])
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
