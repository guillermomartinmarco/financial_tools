"""L2: estrategia integrada ``zscore_reversion`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "zscore_reversion"
PARAMS = {"n": 3, "threshold": 1.0}


def test_zscore_reversion_behavior() -> None:
    # n=3, threshold=1.0.
    # closes: [10, 10, 10, 8, 11, 12, 10]
    # t=2: z=0. pos=0
    # t=3: close=8, mean=9.33, std=0.94, z=(8-9.33)/0.94 = -1.41. -1.41 < -1.0 -> LONG
    # t=4: close=11, mean=9.66, std=1.24, z=(11-9.66)/1.24 = 1.08. 1.08 > 0 -> FLAT
    # t=5: close=12, mean=10.33, std=1.69, z=(12-10.33)/1.69 = 0.98. 0.98 > 0 -> FLAT
    # t=6: close=10, mean=11.0, std=0.81, z=(10-11.0)/0.81 = -1.23. -1.23 < -1.0 -> LONG
    pos = builtin_position(KIND, candles_frame([10.0, 10.0, 10.0, 8.0, 11.0, 12.0, 10.0]), **PARAMS)
    assert pos == [0, 0, 0, 1, 0, 0, 1]


def test_warmup_is_n_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(**PARAMS)) == 3
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), **PARAMS)
    assert pos[:3] == [0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    # std=0 -> z=None -> no signal.
    pos = builtin_position(KIND, candles_frame([5.0] * 8), **PARAMS)
    assert pos == [0] * 8


@pytest.mark.parametrize("params", [{"n": 1}, {"n": 10_001}, {"n": 3, "threshold": -0.1}])
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


def test_overbought_opens_short_and_returning_below_zero_closes_it() -> None:
    pos = builtin_position(
        KIND,
        candles_frame([10.0, 10.0, 10.0, 20.0, 15.0, 15.0, 6.0]),
        n=3,
        threshold=1.0,
        allow_short=True,
    )
    assert pos == [0, 0, 0, -1, -1, 0, 1]
