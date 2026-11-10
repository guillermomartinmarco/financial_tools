"""G4: contraste del motor VectorBT de stops contra una referencia Python pura independiente.

Cada escenario construye velas con mechas intrabar explícitas, corre el motor real
(``run_pipeline`` → ``VectorbtEngine``) con un ``RiskModel`` y lo compara, columna a columna
(``trades`` y ``equity`` en ``Float32``, rel=1e-6), contra ``reference.simulate_with_stops``
(bucle barra a barra, sin VectorBT). La referencia es el oráculo independiente que exige el
"Hecho cuando" de la Enmienda G4 para el ítem de riesgo (corrección del motor y del dominio).
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl
import pytest
from finances_core.backtest.engine_api import CostModel
from finances_core.backtest.frames import (
    EXIT_REASON_SIGNAL,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    SIDE_LONG,
    SIDE_SHORT,
    validate_engine_output,
)
from finances_core.schemas.backtest import RiskModel

from tests.contract.reference import (
    Candles,
    Costs,
    simulate_with_stops,
    stops_trades_frame,
)
from tests.ft_vectorbt.conftest import make_candles, make_signals, run_pipeline, times

REL = 1e-6
NO_COSTS = Costs(initial_cash=1000.0, size_fraction=1.0, fees_bps=0.0, slippage_bps=0.0)
WITH_COSTS = Costs(initial_cash=1000.0, size_fraction=1.0, fees_bps=10.0, slippage_bps=5.0)


def _candles_obj(
    open_: Sequence[float],
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
) -> Candles:
    n = len(open_)
    return Candles(
        open_time_ms=tuple(times(n)),
        open=tuple(float(x) for x in open_),
        high=tuple(float(x) for x in high),
        low=tuple(float(x) for x in low),
        close=tuple(float(x) for x in close),
    )


def _assert_frames_match(label: str, actual: pl.DataFrame, expected: pl.DataFrame) -> None:
    assert actual.columns == expected.columns, label
    assert actual.height == expected.height, f"{label}: filas {actual.height} != {expected.height}"
    for col in actual.columns:
        a = actual[col].to_list()
        e = expected[col].to_list()
        if actual.schema[col] == pl.Int64 or actual.schema[col] == pl.Int8:
            assert a == e, f"{label}: columna {col}: {a} != {e}"
        else:
            assert a == pytest.approx(e, rel=REL), f"{label}: columna {col}: {a} != {e}"


def _run_and_compare(
    label: str,
    open_: Sequence[float],
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    *,
    risk: RiskModel,
    costs: Costs = NO_COSTS,
    entries: Sequence[int] = (),
    exits: Sequence[int] = (),
    entries_short: Sequence[int] = (),
    exits_short: Sequence[int] = (),
    expected_reason: int,
    compare_equity: bool = True,
) -> dict:
    n = len(open_)
    candles = make_candles(list(open_), list(close), high=list(high), low=list(low))
    out = run_pipeline(
        candles,
        make_signals(
            n,
            entries=entries,
            exits=exits,
            entries_short=entries_short,
            exits_short=exits_short,
        ),
        costs=CostModel(fees_bps=costs.fees_bps, slippage_bps=costs.slippage_bps),
        size_fraction=costs.size_fraction,
        initial_cash=costs.initial_cash,
        risk=risk,
    )
    validate_engine_output(out, n)

    def flags(idx: Sequence[int]) -> list[bool]:
        return [i in idx for i in range(n)]

    sim = simulate_with_stops(
        _candles_obj(open_, high, low, close),
        flags(entries),
        flags(exits),
        flags(entries_short),
        flags(exits_short),
        costs,
        risk,
    )
    expected_trades = stops_trades_frame(sim)
    _assert_frames_match(label, out.trades, expected_trades)
    assert out.trades.height == 1, f"{label}: se esperaba una operación"
    assert out.trades["exit_reason"].to_list() == [expected_reason], label
    if compare_equity:
        expected_equity = pl.DataFrame(
            {
                "open_time_ms": pl.Series(times(n), dtype=pl.Int64),
                "equity": pl.Series(sim.equity, dtype=pl.Float32),
                "drawdown": pl.Series(sim.drawdown, dtype=pl.Float32),
            }
        )
        _assert_frames_match(f"{label}/equity", out.equity, expected_equity)
    return out.trades.to_dicts()[0]


FLAT = [100.0, 100.0, 100.0, 100.0, 100.0]


def test_long_stop_loss_matches_reference() -> None:
    _run_and_compare(
        "long-sl",
        FLAT,
        [101, 101, 101, 101, 101],
        [99, 99, 85, 99, 99],
        FLAT,
        risk=RiskModel(sl_frac=0.10),
        entries=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
    )


def test_long_take_profit_matches_reference() -> None:
    _run_and_compare(
        "long-tp",
        FLAT,
        [101, 101, 115, 101, 101],
        [99, 99, 99, 99, 99],
        FLAT,
        risk=RiskModel(tp_frac=0.10),
        entries=(0,),
        expected_reason=EXIT_REASON_TAKE_PROFIT,
    )


def test_short_stop_loss_matches_reference() -> None:
    _run_and_compare(
        "short-sl",
        FLAT,
        [101, 101, 115, 101, 101],
        [99, 99, 99, 99, 99],
        FLAT,
        risk=RiskModel(sl_frac=0.10),
        entries_short=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
        compare_equity=False,
    )


def test_short_take_profit_matches_reference() -> None:
    _run_and_compare(
        "short-tp",
        FLAT,
        [101, 101, 101, 101, 101],
        [99, 99, 85, 99, 99],
        FLAT,
        risk=RiskModel(tp_frac=0.10),
        entries_short=(0,),
        expected_reason=EXIT_REASON_TAKE_PROFIT,
        compare_equity=False,
    )


def test_trailing_long_locks_profit_matches_reference() -> None:
    trade = _run_and_compare(
        "trailing-long",
        [100.0, 100.0, 120.0, 118.0, 100.0],
        [101, 110, 130, 119, 109],
        [99, 99, 118, 115, 100],
        [100.0, 105.0, 125.0, 118.0, 108.0],
        risk=RiskModel(sl_frac=0.10, sl_trail=True),
        entries=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
    )
    assert trade["exit_price"] > trade["entry_price"]  # trailing asegura ganancia


def test_trailing_short_locks_profit_matches_reference() -> None:
    trade = _run_and_compare(
        "trailing-short",
        [100.0, 100.0, 80.0, 82.0, 100.0],
        [101, 101, 82, 95, 101],
        [99, 90, 70, 81, 99],
        [100.0, 95.0, 75.0, 82.0, 92.0],
        risk=RiskModel(sl_frac=0.10, sl_trail=True),
        entries_short=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
        compare_equity=False,
    )
    assert trade["exit_price"] < trade["entry_price"]  # corto trailing asegura ganancia


def test_gap_open_beyond_level_fills_at_open_matches_reference() -> None:
    # Barra 2 abre en 80, por debajo del SL (90): llena al open (hueco), no en 90.
    trade = _run_and_compare(
        "gap-long-sl",
        [100.0, 100.0, 80.0, 100.0, 100.0],
        [101, 101, 81, 101, 101],
        [99, 99, 79, 99, 99],
        [100.0, 100.0, 80.0, 100.0, 100.0],
        risk=RiskModel(sl_frac=0.10),
        entries=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
    )
    assert trade["exit_price"] == pytest.approx(80.0, rel=REL)


def test_signal_exit_with_risk_set_but_untouched_matches_reference() -> None:
    _run_and_compare(
        "signal-untouched",
        FLAT,
        [101, 101, 101, 101, 101],
        [99, 99, 99, 99, 99],
        FLAT,
        risk=RiskModel(sl_frac=0.10, tp_frac=0.10),
        entries=(0,),
        exits=(2,),
        expected_reason=EXIT_REASON_SIGNAL,
    )


def test_long_stop_loss_with_costs_fills_without_slippage() -> None:
    # Con costos: la entrada paga deslizamiento (entry>100) pero el stop llena en 90 exacto.
    trade = _run_and_compare(
        "long-sl-costs",
        FLAT,
        [101, 101, 101, 101, 101],
        [99, 99, 85, 99, 99],
        FLAT,
        risk=RiskModel(sl_frac=0.10),
        costs=WITH_COSTS,
        entries=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
    )
    assert trade["entry_price"] > 100.0  # deslizamiento en la entrada
    assert trade["exit_price"] == pytest.approx(90.0, rel=REL)  # stop sin deslizamiento


def test_short_side_is_reported() -> None:
    trade = _run_and_compare(
        "short-side",
        FLAT,
        [101, 101, 115, 101, 101],
        [99, 99, 99, 99, 99],
        FLAT,
        risk=RiskModel(sl_frac=0.10),
        entries_short=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
        compare_equity=False,
    )
    assert trade["side"] == SIDE_SHORT


def test_long_side_is_reported() -> None:
    trade = _run_and_compare(
        "long-side",
        FLAT,
        [101, 101, 101, 101, 101],
        [99, 99, 85, 99, 99],
        FLAT,
        risk=RiskModel(sl_frac=0.10),
        entries=(0,),
        expected_reason=EXIT_REASON_STOP_LOSS,
    )
    assert trade["side"] == SIDE_LONG
