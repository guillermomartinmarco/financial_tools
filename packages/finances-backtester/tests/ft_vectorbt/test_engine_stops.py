"""G4: stops de riesgo (SL/TP/trailing) a nivel de corrida en ``simulate`` con VectorBT.

Casos de 5 barras con mechas intrabar explícitas (``high``/``low``), calculados a mano. Se
verifica la clasificación de ``exit_reason`` (stop-loss vs take-profit vs señal) y el precio
de salida que VectorBT llena (nivel teórico, o la apertura cuando la barra abre más allá del
stop — hueco). ``risk=None`` no cambia nada (cubierto en los goldens de contrato).
"""

from __future__ import annotations

import pytest
from finances_core.backtest.frames import (
    EXIT_REASON_SIGNAL,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    SIDE_LONG,
    SIDE_SHORT,
    validate_engine_output,
)
from finances_core.schemas.backtest import RiskModel

from tests.ft_vectorbt.conftest import make_candles, make_signals, run_pipeline

REL = 1e-6
FLAT = [100.0, 100.0, 100.0, 100.0, 100.0]


def _only_trade(out: object) -> dict:
    rows = out.trades.to_dicts()  # type: ignore[attr-defined]
    assert len(rows) == 1, rows
    return rows[0]


def test_long_stop_loss_fills_at_level_and_classifies() -> None:
    # Entrada en 0 → ejecuta en 1 a open=100 (base cruda). SL 10% → nivel 90.
    # Barra 2: low=85 ≤ 90 → stop-loss, llena en 90 (sin hueco: open=100 > 90).
    candles = make_candles(FLAT, FLAT, high=[101, 101, 101, 101, 101], low=[99, 99, 85, 99, 99])
    out = run_pipeline(candles, make_signals(5, entries=(0,)), risk=RiskModel(sl_frac=0.10))
    validate_engine_output(out, 5)
    trade = _only_trade(out)
    assert trade["side"] == SIDE_LONG
    assert trade["exit_reason"] == EXIT_REASON_STOP_LOSS
    assert trade["exit_price"] == pytest.approx(90.0, rel=REL)


def test_long_take_profit_fills_at_level_and_classifies() -> None:
    # TP 10% → nivel 110. Barra 2: high=115 ≥ 110 → take-profit, llena en 110.
    candles = make_candles(FLAT, FLAT, high=[101, 101, 115, 101, 101], low=[99, 99, 99, 99, 99])
    out = run_pipeline(candles, make_signals(5, entries=(0,)), risk=RiskModel(tp_frac=0.10))
    validate_engine_output(out, 5)
    trade = _only_trade(out)
    assert trade["exit_reason"] == EXIT_REASON_TAKE_PROFIT
    assert trade["exit_price"] == pytest.approx(110.0, rel=REL)


def test_short_stop_loss_classifies() -> None:
    # Corto: entrada en 0 → ejecuta en 1 a 100. SL 10% de un corto → nivel 110 (sube en contra).
    # Barra 2: high=115 ≥ 110 → stop-loss.
    candles = make_candles(FLAT, FLAT, high=[101, 101, 115, 101, 101], low=[99, 99, 99, 99, 99])
    out = run_pipeline(candles, make_signals(5, entries_short=(0,)), risk=RiskModel(sl_frac=0.10))
    validate_engine_output(out, 5)
    trade = _only_trade(out)
    assert trade["side"] == SIDE_SHORT
    assert trade["exit_reason"] == EXIT_REASON_STOP_LOSS
    assert trade["exit_price"] == pytest.approx(110.0, rel=REL)


def test_short_take_profit_classifies() -> None:
    # Corto TP 10% → nivel 90 (baja a favor). Barra 2: low=85 ≤ 90 → take-profit.
    candles = make_candles(FLAT, FLAT, high=[101, 101, 101, 101, 101], low=[99, 99, 85, 99, 99])
    out = run_pipeline(candles, make_signals(5, entries_short=(0,)), risk=RiskModel(tp_frac=0.10))
    validate_engine_output(out, 5)
    trade = _only_trade(out)
    assert trade["side"] == SIDE_SHORT
    assert trade["exit_reason"] == EXIT_REASON_TAKE_PROFIT
    assert trade["exit_price"] == pytest.approx(90.0, rel=REL)


def test_trailing_stop_locks_profit_and_classifies_as_stop_loss() -> None:
    # Trailing largo SL 10%. Sube: barra 2 high=130 (pico). Barra 3 low=115 ≤ 130·0.9=117
    # → stop-loss en 117, por encima de la entrada (100): el trailing asegura ganancia pero
    # sigue siendo STOP_LOSS, no TAKE_PROFIT.
    candles = make_candles(
        [100.0, 100.0, 120.0, 118.0, 100.0],
        [100.0, 105.0, 125.0, 118.0, 108.0],
        high=[101, 110, 130, 119, 109],
        low=[99, 99, 118, 115, 100],
    )
    out = run_pipeline(
        candles, make_signals(5, entries=(0,)), risk=RiskModel(sl_frac=0.10, sl_trail=True)
    )
    validate_engine_output(out, 5)
    trade = _only_trade(out)
    assert trade["exit_reason"] == EXIT_REASON_STOP_LOSS
    assert trade["exit_price"] == pytest.approx(117.0, rel=REL)
    assert trade["exit_price"] > trade["entry_price"]  # ganancia asegurada


def test_multi_trade_distinct_reasons() -> None:
    # Clasificación POR OPERACIÓN: dos trades en una corrida con motivos distintos.
    # entrada 0 → ejecuta 1 (raw=100, TP=110): bar2 high=115 → TP en 110.
    # entrada 2 → ejecuta 3 (raw=100, SL=90): bar4 low=85 → SL en 90.
    candles = make_candles(
        [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
        [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
        high=[101, 101, 115, 101, 101, 101],
        low=[99, 99, 99, 99, 85, 99],
    )
    out = run_pipeline(
        candles,
        make_signals(6, entries=(0, 2)),
        risk=RiskModel(sl_frac=0.10, tp_frac=0.10),
    )
    validate_engine_output(out, 6)
    rows = out.trades.to_dicts()
    assert [r["exit_reason"] for r in rows] == [EXIT_REASON_TAKE_PROFIT, EXIT_REASON_STOP_LOSS]
    assert rows[0]["exit_price"] == pytest.approx(110.0, rel=REL)
    assert rows[1]["exit_price"] == pytest.approx(90.0, rel=REL)


def test_empty_risk_model_equals_no_risk() -> None:
    # RiskModel() sin fracciones no aplica stops: byte a byte igual a risk=None (aditividad).
    candles = make_candles(
        [100.0, 101.0, 102.0, 103.0, 104.0],
        [100.5, 101.5, 102.5, 103.5, 104.5],
        high=[101, 102, 103, 104, 105],
        low=[99, 100, 101, 102, 103],
    )
    signals = make_signals(5, entries=(0,), exits=(3,))
    none_out = run_pipeline(candles, signals, risk=None)
    empty_out = run_pipeline(candles, signals, risk=RiskModel())
    assert none_out.trades.equals(empty_out.trades)
    assert none_out.equity.equals(empty_out.equity)


def test_gap_up_over_tp_with_sl_also_hit_fills_stop_loss() -> None:
    # Barra 2 abre en 112 (por encima del TP 110) y además su low=85 cruza el SL 90.
    # VectorBT prioriza el stop-loss: llena en 90 con exit_reason STOP_LOSS (pérdida honesta).
    candles = make_candles(
        [100.0, 100.0, 112.0, 100.0, 100.0],
        [100.0, 100.0, 100.0, 100.0, 100.0],
        high=[101, 101, 115, 101, 101],
        low=[99, 99, 85, 99, 99],
    )
    out = run_pipeline(
        candles, make_signals(5, entries=(0,)), risk=RiskModel(sl_frac=0.10, tp_frac=0.10)
    )
    validate_engine_output(out, 5)
    trade = _only_trade(out)
    assert trade["exit_reason"] == EXIT_REASON_STOP_LOSS
    assert trade["exit_price"] == pytest.approx(90.0, rel=REL)


def test_signal_exit_still_classified_when_stop_not_touched() -> None:
    # Con risk definido pero sin tocar ningún stop, una salida por señal sigue siendo SIGNAL.
    candles = make_candles(FLAT, FLAT, high=[101, 101, 101, 101, 101], low=[99, 99, 99, 99, 99])
    out = run_pipeline(
        candles,
        make_signals(5, entries=(0,), exits=(2,)),
        risk=RiskModel(sl_frac=0.10, tp_frac=0.10),
    )
    validate_engine_output(out, 5)
    trade = _only_trade(out)
    assert trade["exit_reason"] == EXIT_REASON_SIGNAL
