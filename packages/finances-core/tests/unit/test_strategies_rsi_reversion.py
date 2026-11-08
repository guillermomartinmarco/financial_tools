"""B1.8: estrategia integrada ``rsi_reversion`` (RF-2, RF-17, RF-20).

Caso de 10 barras calculado a mano con ``period = 2``, ``lower = 30``, ``upper = 70``
(el RSI de cada barra está desarrollado en ``test_strategies_lib.py``):

====  =====  =======  ==========================================
 t    close  RSI      señal
====  =====  =======  ==========================================
 0    10     —        calentamiento
 1    11     —        calentamiento
 2    12     100      calentamiento (semilla; t < period + 1)
 3    11     50       — (100 > 70: no es cruce al alza)
 4    9      16.67    entry (50 >= 30 y 16.67 < 30)
 5    10     50       —
 6    13     85.29    exit (50 <= 70 y 85.29 > 70)
 7    13     85.29    —
 8    13     85.29    —
 9    10     12.83    entry (85.29 >= 30 y 12.83 < 30)
====  =====  =======  ==========================================
"""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "rsi_reversion"
CLOSES = [10.0, 11.0, 12.0, 11.0, 9.0, 10.0, 13.0, 13.0, 13.0, 10.0]
PARAMS = {"period": 2, "lower": 30.0, "upper": 70.0}


def test_hand_computed_ten_bars() -> None:
    # entradas en t=4, 9 y salida en t=6 -> posición sostenida (largo/plano):
    pos = builtin_position(KIND, candles_frame(CLOSES), **PARAMS)
    assert pos == [0, 0, 0, 0, 1, 1, 0, 0, 0, 1]


def test_warmup_is_period_plus_one() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(**PARAMS)) == 3


def test_flat_series_rsi_50_has_no_signal() -> None:
    pos = builtin_position(KIND, candles_frame([7.0] * 10), **PARAMS)
    assert pos == [0] * 10


def test_only_gains_rsi_100_has_no_signal() -> None:
    # RSI = 100 desde la semilla: nunca hay una barra previa <= upper
    closes = [float(10 + i) for i in range(10)]
    pos = builtin_position(KIND, candles_frame(closes), **PARAMS)
    assert pos == [0] * 10


@pytest.mark.parametrize(
    "params",
    [
        {"period": 14, "lower": 70.0, "upper": 30.0},
        {"period": 14, "lower": 50.0, "upper": 50.0},
        {"period": 14, "lower": 0.0, "upper": 70.0},
        {"period": 14, "lower": 30.0, "upper": 100.0},
        {"period": 1, "lower": 30.0, "upper": 70.0},
        {"period": 1001, "lower": 30.0, "upper": 70.0},
    ],
)
def test_invalid_params(params: dict[str, float]) -> None:
    registry = get_registry(FinancesSettings())
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": params}, registry)
    assert info.value.code == "invalid_params"


def test_lower_not_below_upper_names_the_fields() -> None:
    registry = get_registry(FinancesSettings())
    params = {"period": 14, "lower": 70.0, "upper": 30.0}
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": params}, registry)
    assert "lower" in str(info.value)
    assert "upper" in str(info.value)


def test_readme_sections_diagram_and_table() -> None:
    folder = BUILTIN_DIR / KIND
    check_structure(folder)
    strategy = get_registry(FinancesSettings()).get(KIND)
    check_params_table(folder, strategy.Params, strategy.default_params)
    assert get_registry(FinancesSettings()).origins[KIND] == "builtin"


def test_assert_strategy_contract() -> None:
    assert_strategy_contract(KIND)
