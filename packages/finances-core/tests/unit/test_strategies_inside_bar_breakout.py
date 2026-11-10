"""L5: estrategia integrada ``inside_bar_breakout`` (RF-2, RF-17, RF-20)."""

from __future__ import annotations

from backtest_helpers import builtin_position, ohlc_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "inside_bar_breakout"


def test_breakout_above_an_inside_bar_enters_long() -> None:
    # t=2 es inside bar (10.5 < high[1]=11 y 10.2 > low[1]=10); t=3 rompe su máximo (10.5).
    pos = builtin_position(
        KIND,
        ohlc_frame(
            high=[10.0, 11.0, 10.5, 11.5, 12.0],
            low=[9.0, 10.0, 10.2, 11.0, 12.0],
            close=[10.0, 11.0, 10.5, 11.5, 12.0],
        ),
    )
    assert pos == [0, 0, 0, 1, 1]


def test_no_inside_bar_has_no_signal() -> None:
    # Tendencia monótona: ninguna barra queda contenida en el rango de la anterior.
    pos = builtin_position(
        KIND,
        ohlc_frame(
            high=[10.0, 11.0, 12.0, 13.0, 14.0],
            low=[9.0, 10.0, 11.0, 12.0, 13.0],
            close=[10.0, 11.0, 12.0, 13.0, 14.0],
        ),
    )
    assert pos == [0] * 5


def test_warmup_is_two_and_has_no_signal() -> None:
    strategy = get_registry(FinancesSettings()).get(KIND)
    assert strategy.warmup(strategy.Params()) == 2
    pos = builtin_position(KIND, ohlc_frame(high=[1.0, 2.0], low=[1.0, 2.0], close=[1.0, 2.0]))
    assert pos == [0, 0]


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(KIND, ohlc_frame(high=[5.0] * 8, low=[5.0] * 8, close=[5.0] * 8))
    assert pos == [0] * 8


def test_registered_as_builtin_with_no_params() -> None:
    registry = get_registry(FinancesSettings())
    assert registry.origins[KIND] == "builtin"
    strategy = registry.get(KIND)
    assert strategy.default_params == {}
    assert strategy.Params.model_validate({})


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
    assert strategy.Params.model_validate({}).allow_short is False


def test_downside_breakout_of_an_inside_bar_opens_short_when_allow_short() -> None:
    # t=2 es inside bar; t=3 rompe su m\u00ednimo a la baja -> abre corto en vez de aplanar.
    pos = builtin_position(
        KIND,
        ohlc_frame(
            high=[10.0, 11.0, 10.5, 9.0], low=[9.0, 10.0, 10.2, 8.5], close=[10.0, 11.0, 10.5, 8.8]
        ),
        allow_short=True,
    )
    assert pos == [0, 0, 0, -1]
