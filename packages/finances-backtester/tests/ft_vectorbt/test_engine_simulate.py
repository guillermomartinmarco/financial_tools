"""V1.2: ``simulate`` con VectorBT según RF-2 y ``finances_backtester`` RF-5 (reglas 1–7).

Casos de 5–10 barras calculados a mano (aritmética explícita en cada test, sin la
referencia de la suite de contrato). Se recorren los tres puntos públicos de ``engine.py``
(``run_pipeline``) y se comparan **todos** los campos de ``trades`` y ``equity``; las
magnitudes llegan en ``Float32`` (RF-6), por eso la comparación es relativa a 1e-6.
"""

from __future__ import annotations

from typing import Any

import pytest
from finances_core.backtest.engine_api import CostModel
from finances_core.backtest.frames import (
    EXIT_REASON_END_OF_DATA,
    EXIT_REASON_SIGNAL,
    SIDE_LONG,
    validate_engine_output,
)

from finances_backtester.ft_vectorbt.engine import simulate, to_framework_input
from tests.ft_vectorbt.conftest import make_candles, make_signals, run_pipeline, times

REL = 1e-6
FEES_10_SLIP_5 = CostModel(fees_bps=10.0, slippage_bps=5.0)


def _approx_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: v if isinstance(v, int) else pytest.approx(v, rel=REL) for k, v in row.items()}
        for row in rows
    ]


def _drawdown(equity: list[float], initial_cash: float) -> list[float]:
    peak, out = initial_cash, []
    for value in equity:
        peak = max(peak, value)
        out.append(value / peak - 1.0)
    return out


def test_no_costs_shift_and_execution_at_open() -> None:
    """Señal de entrada al cierre de 0 → compra en la apertura de 1; salida en 3 → venta en 4."""
    open_ = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
    close = [101.0, 103.0, 100.0, 107.0, 109.0, 111.0]
    out = run_pipeline(make_candles(open_, close), make_signals(6, entries=(0,), exits=(3,)))
    validate_engine_output(out, 6)

    size = 1000.0 / 102.0
    pnl = (108.0 - 102.0) * size
    assert out.trades.to_dicts() == _approx_rows(
        [
            {
                "entry_time_ms": times(6)[1],
                "exit_time_ms": times(6)[4],
                "side": SIDE_LONG,
                "entry_price": 102.0,
                "exit_price": 108.0,
                "size": size,
                "fees": 0.0,
                "pnl": pnl,
                "return": pnl / (102.0 * size),
                "exit_reason": EXIT_REASON_SIGNAL,
            }
        ]
    )
    equity = [1000.0, size * 103.0, size * 100.0, size * 107.0, 1000.0 + pnl, 1000.0 + pnl]
    assert out.equity["open_time_ms"].to_list() == times(6)
    assert out.equity["equity"].to_list() == pytest.approx(equity, rel=REL)
    assert out.equity["drawdown"].to_list() == pytest.approx(
        _drawdown(equity, 1000.0), rel=REL, abs=1e-7
    )
    assert out.equity["drawdown"][2] == pytest.approx(100.0 / 103.0 - 1.0, rel=REL)


def test_costs_size_fraction_and_final_liquidation_at_close() -> None:
    """Comisión 10 bps, deslizamiento 5 bps, ``size_fraction`` 0,5; posición abierta al final."""
    open_ = [50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0]
    close = [50.5, 51.5, 52.5, 53.5, 54.5, 55.5, 57.0]
    out = run_pipeline(
        make_candles(open_, close),
        make_signals(7, entries=(1,)),
        FEES_10_SLIP_5,
        size_fraction=0.5,
        initial_cash=2000.0,
    )
    validate_engine_output(out, 7)

    fee, slip = 0.001, 0.0005
    entry_price = 52.0 * (1 + slip)
    size = 0.5 * 2000.0 / (entry_price * (1 + fee))  # RF-5.5
    entry_fee = fee * entry_price * size
    cash = 2000.0 - entry_price * size - entry_fee
    exit_price = 57.0 * (1 - slip)  # cierre de la última barra con coste de venta
    exit_fee = fee * exit_price * size
    pnl = (exit_price - entry_price) * size - entry_fee - exit_fee
    assert out.trades.to_dicts() == _approx_rows(
        [
            {
                "entry_time_ms": times(7)[2],
                "exit_time_ms": times(7)[6],  # liquidación final: la última barra
                "side": SIDE_LONG,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size": size,
                "fees": entry_fee + exit_fee,
                "pnl": pnl,
                "return": pnl / (entry_price * size + entry_fee),
                "exit_reason": EXIT_REASON_END_OF_DATA,
            }
        ]
    )
    assert entry_fee == pytest.approx(fee * 0.5 * 2000.0 / (1 + fee))
    equity = [2000.0, 2000.0] + [cash + size * c for c in close[2:6]] + [2000.0 + pnl]
    assert out.equity["equity"].to_list() == pytest.approx(equity, rel=REL)
    assert out.equity["drawdown"].to_list() == pytest.approx(
        _drawdown(equity, 2000.0), rel=REL, abs=1e-7
    )


def test_full_size_fraction_leaves_no_cash_and_fees_both_sides() -> None:
    """``size_fraction = 1``: la caja cubre la comisión y queda en 0 tras entrar (RF-5.5)."""
    open_ = [10.0, 10.0, 12.0, 11.0, 13.0]
    close = [10.0, 11.0, 12.5, 11.5, 13.5]
    out = run_pipeline(
        make_candles(open_, close),
        make_signals(5, entries=(0,), exits=(2,)),
        CostModel(fees_bps=25.0, slippage_bps=0.0),
        size_fraction=1.0,
        initial_cash=100.0,
    )
    fee = 0.0025
    size = 100.0 / (10.0 * (1 + fee))
    entry_fee = fee * 10.0 * size
    exit_fee = fee * 11.0 * size
    pnl = (11.0 - 10.0) * size - entry_fee - exit_fee
    assert out.trades.to_dicts() == _approx_rows(
        [
            {
                "entry_time_ms": times(5)[1],
                "exit_time_ms": times(5)[3],
                "side": SIDE_LONG,
                "entry_price": 10.0,
                "exit_price": 11.0,
                "size": size,
                "fees": entry_fee + exit_fee,
                "pnl": pnl,
                "return": pnl / (10.0 * size + entry_fee),
                "exit_reason": EXIT_REASON_SIGNAL,
            }
        ]
    )
    # Caja 0 tras entrar: la equity de las barras 1 y 2 es solo la posición.
    equity = [100.0, size * 11.0, size * 12.5, 100.0 + pnl, 100.0 + pnl]
    assert out.equity["equity"].to_list() == pytest.approx(equity, rel=REL)


@pytest.mark.parametrize("entry_bar", [4, 5])
def test_entries_that_would_execute_on_or_after_the_last_bar_are_discarded(
    entry_bar: int,
) -> None:
    """Señal en la penúltima barra (se ejecutaría en la última) o en la última: sin operación."""
    n = 6
    out = run_pipeline(
        make_candles([10.0 + i for i in range(n)], [10.5 + i for i in range(n)]),
        make_signals(n, entries=(entry_bar,)),
        FEES_10_SLIP_5,
    )
    validate_engine_output(out, n)
    assert out.trades.height == 0
    assert out.equity["equity"].to_list() == [1000.0] * n
    assert out.equity["drawdown"].to_list() == [0.0] * n


def test_entry_with_open_position_is_ignored_and_no_accumulation() -> None:
    """Segunda entrada con posición abierta: ignorada (sin acumular)."""
    open_ = [20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0]
    close = [20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0]
    out = run_pipeline(make_candles(open_, close), make_signals(8, entries=(0, 2, 3), exits=(4,)))
    validate_engine_output(out, 8)

    size = 1000.0 / 21.0
    pnl = (25.0 - 21.0) * size
    assert out.trades.to_dicts() == _approx_rows(
        [
            {
                "entry_time_ms": times(8)[1],
                "exit_time_ms": times(8)[5],
                "side": SIDE_LONG,
                "entry_price": 21.0,
                "exit_price": 25.0,
                "size": size,
                "fees": 0.0,
                "pnl": pnl,
                "return": pnl / (21.0 * size),
                "exit_reason": EXIT_REASON_SIGNAL,
            }
        ]
    )
    equity = [1000.0] + [size * c for c in close[1:5]] + [1000.0 + pnl] * 3
    assert out.equity["equity"].to_list() == pytest.approx(equity, rel=REL)


def test_exit_while_flat_is_ignored_and_reentry_after_exit() -> None:
    """Salida sin posición ignorada; entrada en la barra de la salida → nueva operación."""
    open_ = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
    close = open_
    out = run_pipeline(
        make_candles(open_, close),
        make_signals(10, entries=(2, 5), exits=(0, 4, 7)),
    )
    validate_engine_output(out, 10)

    size_1 = 1000.0 / 13.0
    cash_1 = size_1 * 15.0  # la salida al cierre de 4 se ejecuta en la apertura de 5
    size_2 = cash_1 / 16.0  # la entrada al cierre de 5 se ejecuta en la apertura de 6
    rows = out.trades.to_dicts()
    assert [(r["entry_time_ms"], r["exit_time_ms"]) for r in rows] == [
        (times(10)[3], times(10)[5]),
        (times(10)[6], times(10)[8]),
    ]
    assert rows[0]["size"] == pytest.approx(size_1, rel=REL)
    assert rows[1]["size"] == pytest.approx(size_2, rel=REL)
    assert rows[1]["pnl"] == pytest.approx((18.0 - 16.0) * size_2, rel=REL)
    assert [r["exit_reason"] for r in rows] == [EXIT_REASON_SIGNAL, EXIT_REASON_SIGNAL]


def test_exit_signal_on_penultimate_bar_sells_at_last_open_not_end_of_data() -> None:
    """La salida que cae en la última barra se ejecuta a su apertura (``exit_reason = 0``)."""
    open_ = [30.0, 31.0, 32.0, 33.0, 34.0]
    close = [30.5, 31.5, 32.5, 33.5, 40.0]
    out = run_pipeline(
        make_candles(open_, close),
        make_signals(5, entries=(0,), exits=(3,)),
        FEES_10_SLIP_5,
    )
    slip = 0.0005
    rows = out.trades.to_dicts()
    assert len(rows) == 1
    assert rows[0]["exit_time_ms"] == times(5)[4]
    assert rows[0]["exit_price"] == pytest.approx(34.0 * (1 - slip), rel=REL)
    assert rows[0]["exit_reason"] == EXIT_REASON_SIGNAL


def test_single_bar_has_no_trades() -> None:
    out = run_pipeline(make_candles([10.0], [11.0]), make_signals(1, entries=(0,)))
    validate_engine_output(out, 1)
    assert out.trades.height == 0
    assert out.equity["equity"].to_list() == [1000.0]


def test_simulate_does_not_mutate_its_input() -> None:
    candles = make_candles([10.0, 11.0, 12.0, 13.0, 14.0], [10.0, 11.0, 12.0, 13.0, 14.5])
    inp = to_framework_input(candles, make_signals(5, entries=(0, 3), exits=(1,)))
    before = (inp.open.copy(), inp.close.copy(), inp.entries.copy(), inp.exits.copy())
    simulate(inp, FEES_10_SLIP_5, 1.0, 1000.0)
    after = (inp.open, inp.close, inp.entries, inp.exits)
    assert all((a == b).all() for a, b in zip(before, after, strict=True))
