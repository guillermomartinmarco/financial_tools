"""L2: estrategia integrada ``atr_channel_breakout`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

import polars as pl
import pytest
from backtest_helpers import builtin_position, candles_frame, ohlc_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "atr_channel_breakout"
PARAMS = {"n": 5, "k": 0.5}


def test_atr_channel_breakout_behavior() -> None:
    # n=5, k=0.5.
    # t=5: close=12, open=11, atr=0.4. 12 > 11 + 0.5*0.4=11.2 -> LONG
    # t=6: close=10, open=11, atr=0.8. 10 < 11 - 0.5*0.8=10.6 -> FLAT
    # t=7: close=8, open=9, atr=1.2. 8 < 9 - 0.5*1.2=8.4 -> FLAT
    # ohlc_frame sets open=close. We need custom open.
    candles = ohlc_frame(
        high=[10.0] * 5 + [12.0, 12.0, 10.0],
        low=[10.0] * 5 + [11.0, 10.0, 8.0],
        close=[10.0] * 5 + [12.0, 10.0, 8.0],
    )
    # Override open
    candles = candles.with_columns(pl.Series("open", [10.0] * 5 + [11.0, 11.0, 9.0]))
    pos = builtin_position(KIND, candles, **PARAMS)
    assert pos == [0, 0, 0, 0, 0, 1, 0, 0]


def test_warmup_is_n_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(**PARAMS)) == 5
    pos = builtin_position(KIND, candles_frame([1.0, 2.0, 3.0, 4.0, 5.0]), **PARAMS)
    assert pos[:5] == [0, 0, 0, 0, 0]


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(KIND, candles_frame([5.0] * 8), **PARAMS)
    assert pos == [0] * 8


@pytest.mark.parametrize("params", [{"n": 1}, {"n": 10001}, {"k": 0.0}, {"k": 10.1}])
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
    assert strategy.Params.model_validate({"n": 5, "k": 0.5}).allow_short is False


def test_downside_breakout_opens_short_when_allow_short() -> None:
    candles = ohlc_frame(
        high=[10.0] * 5 + [12.0, 12.0, 10.0],
        low=[10.0] * 5 + [11.0, 10.0, 8.0],
        close=[10.0] * 5 + [12.0, 10.0, 8.0],
    )
    candles = candles.with_columns(pl.Series("open", [10.0] * 5 + [11.0, 11.0, 9.0]))
    pos = builtin_position(KIND, candles, n=5, k=0.5, allow_short=True)
    assert pos == [0, 0, 0, 0, 0, 1, 0, -1]
