"""V1.1: puntos de conversión de ``engine.py`` (RF-3, RFC-0004 puntos 2 y 3).

- Ida y vuelta ``Float32`` → doble precisión → ``Float32`` idéntica bit a bit.
- ``entries``/``exits`` ``Boolean`` → arreglos booleanos idénticos.
- ``open_time_ms`` nunca entra al framework: índice posicional y tiempos recuperados por
  posición desde la ``Series`` ``Int64`` original.
- Salida con ``NaN``/``inf``, ``side`` o ``exit_reason`` fuera de dominio, o índices fuera
  del rango → ``output_out_of_range``.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import polars as pl
import pytest
from finances_core.backtest.errors import OutputOutOfRangeError
from finances_core.backtest.frames import (
    EQUITY_SCHEMA,
    EXIT_REASON_END_OF_DATA,
    EXIT_REASON_SIGNAL,
    SIDE_LONG,
    TRADES_SCHEMA,
    validate_engine_output,
)

from finances_backtester.ft_vectorbt.engine import (
    FrameworkInput,
    FrameworkResult,
    from_framework_output,
    to_framework_input,
)
from tests.ft_vectorbt.conftest import make_candles, make_signals, times

#: Valores ``float32`` difíciles: decimales no exactos, extremos y subnormales.
AWKWARD_F32 = [
    0.1,
    1.0 / 3.0,
    1e-7,
    float(np.finfo(np.float32).max),
    float(np.finfo(np.float32).tiny),
    float(np.float32(1e-45)),  # subnormal
    65_504.125,
    12_345.678,
]


def _bits(series: pl.Series) -> bytes:
    return series.to_numpy().tobytes()


def _result(n_bars: int, **overrides: Any) -> FrameworkResult:
    """``FrameworkResult`` válido de una operación (barras 1 → 3) sobre ``n_bars``."""
    fields: dict[str, Any] = {
        "entry_idx": np.array([1]),
        "exit_idx": np.array([3]),
        "side": np.array([SIDE_LONG]),
        "entry_price": np.array([100.0]),
        "exit_price": np.array([110.0]),
        "size": np.array([2.0]),
        "fees": np.array([0.5]),
        "pnl": np.array([19.5]),
        "return_": np.array([19.5 / 200.25]),
        "exit_reason": np.array([EXIT_REASON_SIGNAL]),
        "equity": np.full(n_bars, 1000.0),
        "drawdown": np.zeros(n_bars),
    }
    fields.update(overrides)
    return FrameworkResult(**fields)


# --------------------------------------------------------------------------- entrada


def test_widening_is_exact_and_round_trip_is_bit_identical() -> None:
    candles = make_candles(AWKWARD_F32, list(reversed(AWKWARD_F32)))
    signals = make_signals(len(AWKWARD_F32))
    inp = to_framework_input(candles, signals)

    assert inp.open.dtype == np.dtype("f8")
    assert inp.close.dtype == np.dtype("f8")
    assert inp.open.tolist() == candles["open"].to_list()
    assert inp.close.tolist() == candles["close"].to_list()

    # Vuelta por el punto 2: la equity de la salida es el close ampliado.
    n = candles.height
    out = from_framework_output(
        _result(n, equity=inp.close, entry_idx=np.array([0]), exit_idx=np.array([n - 1])),
        candles["open_time_ms"],
    )
    assert out.equity["equity"].dtype == pl.Float32
    assert _bits(out.equity["equity"]) == _bits(candles["close"])


def test_boolean_signals_convert_without_loss() -> None:
    n = 9
    signals = make_signals(n, entries=(0, 3, 8), exits=(1, 5))
    inp = to_framework_input(make_candles([1.0] * n, [1.0] * n), signals)

    assert inp.entries.dtype == np.bool_
    assert inp.exits.dtype == np.bool_
    assert inp.entries.tolist() == signals.entries.to_list()
    assert inp.exits.tolist() == signals.exits.to_list()


def test_open_time_never_enters_the_framework() -> None:
    n = 5
    inp = to_framework_input(make_candles([1.0] * n, [1.0] * n), make_signals(n))

    assert {f.name for f in dataclasses.fields(FrameworkInput)} == {
        "open",
        "close",
        "entries",
        "exits",
        "short_entries",
        "short_exits",
    }
    assert inp.n_bars == n


def test_to_framework_input_rejects_non_float32_prices() -> None:
    candles = make_candles([1.0, 2.0], [1.0, 2.0]).with_columns(pl.col("open").cast(pl.Float64))
    with pytest.raises(TypeError, match="open"):
        to_framework_input(candles, make_signals(2))


def test_to_framework_input_rejects_signals_of_other_length() -> None:
    with pytest.raises(ValueError, match="longitud"):
        to_framework_input(make_candles([1.0, 2.0], [1.0, 2.0]), make_signals(3))


# --------------------------------------------------------------------------- salida


def test_times_are_recovered_by_position_from_the_int64_series() -> None:
    stamps = [5, 17, 1_000_000_000_000_123, 2**62 + 1, 2**62 + 7]
    out = from_framework_output(
        _result(5, entry_idx=np.array([2]), exit_idx=np.array([4])),
        pl.Series("open_time_ms", stamps, dtype=pl.Int64),
    )

    assert out.trades["entry_time_ms"].to_list() == [stamps[2]]
    assert out.trades["exit_time_ms"].to_list() == [stamps[4]]
    assert out.equity["open_time_ms"].to_list() == stamps


def test_output_has_canonical_schemas_and_values() -> None:
    out = from_framework_output(_result(5), pl.Series(times(5), dtype=pl.Int64))

    assert out.trades.schema == TRADES_SCHEMA
    assert out.equity.schema == EQUITY_SCHEMA
    validate_engine_output(out, 5)
    assert out.trades.row(0, named=True) == {
        "entry_time_ms": times(5)[1],
        "exit_time_ms": times(5)[3],
        "side": SIDE_LONG,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "size": 2.0,
        "fees": 0.5,
        "pnl": 19.5,
        "return": pytest.approx(19.5 / 200.25, rel=1e-7),
        "exit_reason": EXIT_REASON_SIGNAL,
    }
    assert out.warnings == []


def test_empty_trades_keep_the_canonical_schema() -> None:
    empty_i = np.array([], dtype=np.int64)
    empty_f = np.array([])
    res = _result(
        3,
        entry_idx=empty_i,
        exit_idx=empty_i,
        side=empty_i,
        entry_price=empty_f,
        exit_price=empty_f,
        size=empty_f,
        fees=empty_f,
        pnl=empty_f,
        return_=empty_f,
        exit_reason=empty_i,
    )
    out = from_framework_output(res, pl.Series(times(3), dtype=pl.Int64))

    assert out.trades.height == 0
    assert out.trades.schema == TRADES_SCHEMA
    validate_engine_output(out, 3)


@pytest.mark.parametrize("field", ["entry_price", "exit_price", "size", "fees", "pnl", "return_"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_trade_values_are_rejected(field: str, bad: float) -> None:
    with pytest.raises(OutputOutOfRangeError, match="output_out_of_range") as info:
        from_framework_output(
            _result(5, **{field: np.array([bad])}), pl.Series(times(5), dtype=pl.Int64)
        )
    assert info.value.code == "output_out_of_range"


@pytest.mark.parametrize("field", ["equity", "drawdown"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_equity_values_are_rejected(field: str, bad: float) -> None:
    values = np.zeros(5) if field == "drawdown" else np.full(5, 1000.0)
    values[2] = bad
    with pytest.raises(OutputOutOfRangeError, match="output_out_of_range"):
        from_framework_output(_result(5, **{field: values}), pl.Series(times(5), dtype=pl.Int64))


def test_value_overflowing_float32_is_rejected_not_truncated() -> None:
    with pytest.raises(OutputOutOfRangeError, match="output_out_of_range"):
        from_framework_output(
            _result(5, pnl=np.array([1e300])), pl.Series(times(5), dtype=pl.Int64)
        )


def test_side_short_is_accepted() -> None:
    # Enmienda G2b: -1 (corto) ya no es out_of_domain.
    output = from_framework_output(
        _result(5, side=np.array([-1])), pl.Series(times(5), dtype=pl.Int64)
    )
    assert output.trades["side"].to_list() == [-1]


@pytest.mark.parametrize("side", [0, 2, 300])
def test_side_out_of_domain_is_rejected(side: int) -> None:
    with pytest.raises(OutputOutOfRangeError, match="side"):
        from_framework_output(
            _result(5, side=np.array([side])), pl.Series(times(5), dtype=pl.Int64)
        )


@pytest.mark.parametrize("reason", [-1, 2, 7, 300])
def test_exit_reason_out_of_domain_is_rejected(reason: int) -> None:
    with pytest.raises(OutputOutOfRangeError, match="exit_reason"):
        from_framework_output(
            _result(5, exit_reason=np.array([reason])), pl.Series(times(5), dtype=pl.Int64)
        )


def test_end_of_data_exit_reason_is_in_domain() -> None:
    out = from_framework_output(
        _result(5, exit_reason=np.array([EXIT_REASON_END_OF_DATA])),
        pl.Series(times(5), dtype=pl.Int64),
    )
    assert out.trades["exit_reason"].to_list() == [EXIT_REASON_END_OF_DATA]


@pytest.mark.parametrize(("field", "value"), [("entry_idx", -1), ("entry_idx", 5), ("exit_idx", 9)])
def test_bar_index_out_of_range_is_rejected(field: str, value: int) -> None:
    with pytest.raises(OutputOutOfRangeError, match=field):
        from_framework_output(
            _result(5, **{field: np.array([value])}), pl.Series(times(5), dtype=pl.Int64)
        )


def test_equity_length_must_match_bars() -> None:
    with pytest.raises(OutputOutOfRangeError, match="equity"):
        from_framework_output(_result(4), pl.Series(times(5), dtype=pl.Int64))
