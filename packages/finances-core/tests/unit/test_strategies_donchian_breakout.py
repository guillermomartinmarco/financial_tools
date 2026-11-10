"""B1.9: estrategia integrada ``donchian_breakout`` (RF-2, RF-17, RF-20).

Caso de 10 barras calculado a mano con ``window = 3``; ``alto(t)``/``bajo(t)`` son el
máximo de ``high`` y el mínimo de ``low`` de ``t-3..t-1`` (excluyen la barra actual):

====  ====  ===  =====  ====  ====  ===========================================
 t    high  low  close  alto  bajo  señal
====  ====  ===  =====  ====  ====  ===========================================
 0    10    9    9.5    —     —     calentamiento
 1    11    10   10.5   —     —     calentamiento (10.5 > 10 con canal parcial)
 2    12    11   11.5   —     —     calentamiento (11.5 > 11 con canal parcial)
 3    11    10   10.5   12    9     —
 4    13    12   13     12    10    entry (con la barra actual: 13 > 13 falso)
 5    12    11   11.5   13    10    —
 6    11    10   10.5   13    10    —
 7    10    9    9.5    13    10    exit (con la barra actual: 9.5 < 9 falso)
 8    9     8    8.5    12    9     exit (condición, no evento)
 9    12    10   12     11    8     entry
====  ====  ===  =====  ====  ====  ===========================================
"""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, ohlc_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "donchian_breakout"
HIGH = [10.0, 11.0, 12.0, 11.0, 13.0, 12.0, 11.0, 10.0, 9.0, 12.0]
LOW = [9.0, 10.0, 11.0, 10.0, 12.0, 11.0, 10.0, 9.0, 8.0, 10.0]
CLOSE = [9.5, 10.5, 11.5, 10.5, 13.0, 11.5, 10.5, 9.5, 8.5, 12.0]


def test_hand_computed_ten_bars() -> None:
    # entrada en t=4, 9 y salidas en t=7, 8 -> posición sostenida (largo/plano):
    pos = builtin_position(KIND, ohlc_frame(high=HIGH, low=LOW, close=CLOSE), window=3)
    assert pos == [0, 0, 0, 0, 1, 1, 1, 0, 0, 1]


def test_channel_including_current_bar_would_change_the_result() -> None:
    # Con el canal sobre t-2..t (incluye la barra actual) close[4] = 13 no supera a
    # max(12, 11, 13) = 13 ni close[7] = 9.5 queda bajo min(11, 10, 9) = 9.
    included_high_4 = max(HIGH[2:5])
    included_low_7 = min(LOW[5:8])
    assert not CLOSE[4] > included_high_4
    assert not CLOSE[7] < included_low_7
    pos = builtin_position(KIND, ohlc_frame(high=HIGH, low=LOW, close=CLOSE), window=3)
    assert pos[3] == 0 and pos[4] == 1  # entrada (flanco) en t=4
    assert pos[6] == 1 and pos[7] == 0  # salida (flanco) en t=7


def test_warmup_is_window_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(window=3)) == 3
    pos = builtin_position(KIND, ohlc_frame(high=HIGH, low=LOW, close=CLOSE), window=3)
    assert pos[:3] == [0, 0, 0]


def test_touching_the_channel_is_not_a_breakout() -> None:
    # close[3] == alto(3) == 12 y close[4] == bajo(4) == 1: empates sin señal
    pos = builtin_position(
        KIND,
        ohlc_frame(
            high=[10.0, 12.0, 11.0, 12.0, 5.0],
            low=[1.0, 1.0, 3.0, 4.0, 1.0],
            close=[5.0, 6.0, 7.0, 12.0, 1.0],
        ),
        window=3,
    )
    assert pos == [0] * 5


@pytest.mark.parametrize("window", [1, 10_001])
def test_invalid_params(window: int) -> None:
    registry = get_registry(FinancesSettings())
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": {"window": window}}, registry)
    assert "params.window" in info.value.errors


def test_readme_sections_diagram_and_table() -> None:
    folder = BUILTIN_DIR / KIND
    check_structure(folder)
    strategy = get_registry(FinancesSettings()).get(KIND)
    check_params_table(folder, strategy.Params, strategy.default_params)
    assert get_registry(FinancesSettings()).origins[KIND] == "builtin"


def test_assert_strategy_contract() -> None:
    assert_strategy_contract(KIND)


# ------------------------------------------------- Enmienda G3: allow_short=True
def test_allow_short_false_by_default_is_byte_identical_to_pre_g3() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.Params.model_validate({"window": 3}).allow_short is False


def test_breakdown_opens_short_when_allow_short() -> None:
    # Misma serie que test_hand_computed_ten_bars, con allow_short=True: la ruptura a
    # la baja abre corto en vez de solo aplanar (verificado ejecutando el c\u00f3digo real).
    pos = builtin_position(
        KIND, ohlc_frame(high=HIGH, low=LOW, close=CLOSE), window=3, allow_short=True
    )
    assert pos == [0, 0, 0, 0, 1, 1, 1, 0, -1, 0]
