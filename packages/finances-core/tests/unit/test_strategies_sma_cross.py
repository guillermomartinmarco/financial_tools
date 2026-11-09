"""B1.7: estrategia integrada ``sma_cross`` (RF-2, RF-17, RF-20).

Caso de 10 barras calculado a mano con ``fast = 2`` y ``slow = 3``:

======  =====  ======  ======  ========================
 t      close  SMA_2   SMA_3   señal
======  =====  ======  ======  ========================
 0      10     —       —       calentamiento
 1      10     10      —       calentamiento
 2      10     10      10      calentamiento (empate)
 3      11     10.5    10.33   entry (empate previo)
 4      10     10.5    10.33   —
 5      8      9       9.67    exit
 6      12     10      10      empate: sin señal
 7      5      8.5     8.33    entry (empate previo)
 8      10     7.5     9       exit
 9      9      9.5     8       entry
======  =====  ======  ======  ========================
"""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "sma_cross"
CLOSES = [10.0, 10.0, 10.0, 11.0, 10.0, 8.0, 12.0, 5.0, 10.0, 9.0]


def test_hand_computed_ten_bars() -> None:
    # entradas en t=3, 7, 9 y salidas en t=5, 8 -> posición sostenida (largo/plano):
    pos = builtin_position(KIND, candles_frame(CLOSES), fast=2, slow=3)
    assert pos == [0, 0, 0, 1, 1, 0, 0, 1, 0, 1]


def test_warmup_is_slow_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params(fast=2, slow=3)) == 3
    # cruce al alza en t=1 y t=2 con una SMA_3 aún no definida: sin señal
    pos = builtin_position(KIND, candles_frame([10.0, 20.0, 30.0, 30.0, 30.0]), fast=2, slow=3)
    assert pos[:3] == [0, 0, 0]


def test_tie_generates_no_signal() -> None:
    # serie plana: SMA_2 == SMA_3 en toda t >= 2, ningún cruce estricto -> plano
    pos = builtin_position(KIND, candles_frame([1.0, 1.0, 1.0, 1.0, 1.0, 1.0]), fast=2, slow=3)
    assert pos == [0] * 6


@pytest.mark.parametrize(("fast", "slow"), [(5, 5), (6, 5), (1, 3), (2, 10_001)])
def test_invalid_params(fast: int, slow: int) -> None:
    registry = get_registry(FinancesSettings())
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": {"fast": fast, "slow": slow}}, registry)
    assert info.value.code == "invalid_params"


def test_fast_not_below_slow_is_reported_by_field_or_model() -> None:
    registry = get_registry(FinancesSettings())
    with pytest.raises(InvalidParamsError) as info:
        parse_strategy_parameters({"kind": KIND, "params": {"fast": 30, "slow": 20}}, registry)
    assert "fast" in str(info.value)


def test_registered_as_builtin_with_defaults() -> None:
    registry = get_registry(FinancesSettings())
    assert registry.origins[KIND] == "builtin"
    strategy = registry.get(KIND)
    assert strategy.Params.model_validate(dict(strategy.default_params))


def test_readme_sections_diagram_and_table() -> None:
    folder = BUILTIN_DIR / KIND
    check_structure(folder)  # 4 secciones, orden, bloque mermaid y tabla
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
