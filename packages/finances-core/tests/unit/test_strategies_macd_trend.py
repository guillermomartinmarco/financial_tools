"""L2: estrategia integrada ``macd_trend`` (RF-2, RF-17, RF-20).

La MACD encadena tres EMAs recursivas; en vez de fijar su recursión a mano (ya cubierta
por ``test_strategies_lib.py::ema``), se comprueba el **comportamiento**: entra largo en
una tendencia alcista sostenida y vuelve a plano cuando se gira. El contrato (prefijo,
anti-anticipación, determinismo, dtype) lo verifica ``assert_strategy_contract``.
"""

from __future__ import annotations

import pytest
from backtest_helpers import builtin_position, candles_frame

from finances_core.backtest.catalog import BUILTIN_DIR, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import InvalidParamsError
from finances_core.backtest.strategies.checks import check_params_table, check_structure
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings

KIND = "macd_trend"
PARAMS = {"fast": 3, "slow": 6, "signal": 3}
# Cuatro barras planas, subida sostenida y bajada sostenida.
TREND = [10.0] * 4 + [10.0 + 2.0 * i for i in range(1, 9)] + [26.0 - 3.0 * i for i in range(1, 8)]


def test_enters_long_in_uptrend_and_exits_on_reversal() -> None:
    pos = builtin_position(KIND, candles_frame(TREND), **PARAMS)
    strategy = get_registry(FinancesSettings()).get(KIND)
    warmup = strategy.warmup(strategy.Params(**PARAMS))
    assert pos[:warmup] == [0] * warmup  # sin señal en el calentamiento
    assert sum(pos) >= 3  # mantiene el largo varias barras en la tendencia
    longs = [t for t, p in enumerate(pos) if p == 1]
    assert longs and all(t >= warmup for t in longs)  # el largo ocurre tras el calentamiento
    assert pos[-1] == 0  # sale a plano en la bajada sostenida


def test_flat_series_has_no_signal() -> None:
    pos = builtin_position(KIND, candles_frame([5.0] * 20), **PARAMS)
    assert pos == [0] * 20


@pytest.mark.parametrize(
    "params",
    [
        {"fast": 6, "slow": 6, "signal": 3},
        {"fast": 7, "slow": 6, "signal": 3},
        {"fast": 3, "slow": 6, "signal": 1},
    ],
)
def test_invalid_params(params: dict[str, int]) -> None:
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


def test_enters_short_on_sustained_downtrend_when_allow_short() -> None:
    pos = builtin_position(KIND, candles_frame(TREND), **PARAMS, allow_short=True)
    strategy = get_registry(FinancesSettings()).get(KIND)
    warmup = strategy.warmup(strategy.Params(**PARAMS, allow_short=True))
    assert pos[:warmup] == [0] * warmup  # sin se\u00f1al en el calentamiento
    shorts = [t for t, p in enumerate(pos) if p == -1]
    assert shorts and all(t >= warmup for t in shorts)  # el corto ocurre tras el calentamiento
    assert pos[-1] == -1  # se sostiene corto en la baja sostenida (verificado por ejecuci\u00f3n)
