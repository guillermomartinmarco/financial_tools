"""G2b: economía de una operación corta frente a una referencia Python pura (RF-5).

``tests/contract/reference.simulate_short_round_trip`` es el oráculo independiente (sin
NumPy/VectorBT) exigido por la regla de verificación para el ítem de riesgo numérico del
motor de cortos (Enmienda G2b): confirma el signo del deslizamiento en ambas patas, el
dimensionamiento por ``size_fraction`` y el ``pnl``/``return`` de un corto, antes de confiar
en el resto de la suite de contrato (que sigue siendo solo larga).
"""

from __future__ import annotations

import pytest
from finances_core.schemas.backtest import CostModel

from tests.contract.reference import Costs, simulate_short_round_trip
from tests.ft_vectorbt.conftest import NO_COSTS, make_candles, make_signals, run_pipeline

# Precio cae de 100 a 90: un corto gana (vende caro, compra barato).
ENTRY_OPEN, EXIT_OPEN = 100.0, 90.0


def _candles() -> object:
    return make_candles(
        open_=[ENTRY_OPEN, ENTRY_OPEN, EXIT_OPEN, EXIT_OPEN, ENTRY_OPEN, ENTRY_OPEN],
        close=[ENTRY_OPEN, ENTRY_OPEN, EXIT_OPEN, EXIT_OPEN, ENTRY_OPEN, ENTRY_OPEN],
    )


def _engine_trade(costs: CostModel) -> dict[str, float]:
    signals = make_signals(6, entries_short=[0], exits_short=[2])
    output = run_pipeline(_candles(), signals, costs=costs, size_fraction=1.0, initial_cash=1000.0)
    assert output.trades.height == 1
    return output.trades.to_dicts()[0]


@pytest.mark.parametrize(
    ("fees_bps", "slippage_bps"), [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
)
def test_short_round_trip_matches_independent_reference(
    fees_bps: float, slippage_bps: float
) -> None:
    costs = CostModel(fees_bps=fees_bps, slippage_bps=slippage_bps)
    expected = simulate_short_round_trip(
        ENTRY_OPEN,
        EXIT_OPEN,
        Costs(initial_cash=1000.0, fees_bps=fees_bps, slippage_bps=slippage_bps),
    )
    got = _engine_trade(costs)

    assert got["side"] == expected.side
    assert got["entry_price"] == pytest.approx(expected.entry_price, rel=1e-6)
    assert got["exit_price"] == pytest.approx(expected.exit_price, rel=1e-6)
    assert got["size"] == pytest.approx(expected.size, rel=1e-4)
    assert got["fees"] == pytest.approx(expected.fees, rel=1e-4)
    assert got["pnl"] == pytest.approx(expected.pnl, rel=1e-4)
    assert got["return"] == pytest.approx(expected.return_, rel=1e-4)


def test_short_entry_slippage_reduces_proceeds_and_exit_slippage_increases_cost() -> None:
    # El deslizamiento SIEMPRE perjudica al operador, en ambos lados (RF-5).
    costs = CostModel(fees_bps=0.0, slippage_bps=10.0)
    got = _engine_trade(costs)
    assert got["entry_price"] < ENTRY_OPEN  # vende para abrir: recibe menos
    assert got["exit_price"] > EXIT_OPEN  # compra para cerrar: paga más


def test_short_profits_when_price_falls() -> None:
    got = _engine_trade(NO_COSTS)
    assert got["pnl"] > 0
    assert got["return"] > 0


def test_short_loses_when_price_rises() -> None:
    signals = make_signals(6, entries_short=[0], exits_short=[2])
    candles = make_candles(
        open_=[90.0, 90.0, 100.0, 100.0, 90.0, 90.0], close=[90.0, 90.0, 100.0, 100.0, 90.0, 90.0]
    )
    output = run_pipeline(candles, signals, costs=NO_COSTS, size_fraction=1.0, initial_cash=1000.0)
    trade = output.trades.to_dicts()[0]
    assert trade["pnl"] < 0
    assert trade["return"] < 0


def test_open_short_at_end_of_data_is_force_liquidated_at_close() -> None:
    # Sin señal de salida: la posición corta abierta se liquida al cierre de la última barra.
    signals = make_signals(4, entries_short=[0])
    candles = make_candles(open_=[100.0, 100.0, 100.0, 90.0], close=[100.0, 100.0, 100.0, 85.0])
    output = run_pipeline(candles, signals, costs=NO_COSTS, size_fraction=1.0, initial_cash=1000.0)
    trade = output.trades.to_dicts()[0]
    assert trade["side"] == -1
    assert trade["exit_reason"] == 1  # end_of_data
    assert trade["exit_price"] == pytest.approx(85.0)  # cierre de la última barra, no su apertura
