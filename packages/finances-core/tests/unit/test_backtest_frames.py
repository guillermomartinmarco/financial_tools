"""B3.1: tablas canónicas ``trades``/``equity`` de RF-6 y ``validate_engine_output``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

HOUR_MS = 3_600_000
N_BARS = 4


@dataclass
class _Output:
    trades: pl.DataFrame
    equity: pl.DataFrame
    warnings: list[str] = field(default_factory=list)


def _trades(**overrides: list[Any]) -> pl.DataFrame:
    data: dict[str, list[Any]] = {
        "entry_time_ms": [0, 2 * HOUR_MS],
        "exit_time_ms": [HOUR_MS, 3 * HOUR_MS],
        "side": [1, 1],
        "entry_price": [100.0, 102.0],
        "exit_price": [101.0, 101.5],
        "size": [99.9, 98.0],
        "fees": [20.0, 19.9],
        "pnl": [79.9, -68.9],
        "return": [0.008, -0.0069],
        "exit_reason": [0, 1],
    }
    data.update(overrides)
    return pl.DataFrame(data, schema=TRADES_SCHEMA)


def _equity(**overrides: list[Any]) -> pl.DataFrame:
    data: dict[str, list[Any]] = {
        "open_time_ms": [0, HOUR_MS, 2 * HOUR_MS, 3 * HOUR_MS],
        "equity": [10_000.0, 10_079.9, 10_050.0, 10_011.0],
        "drawdown": [0.0, 0.0, -0.002966, -0.006835],
    }
    data.update(overrides)
    return pl.DataFrame(data, schema=EQUITY_SCHEMA)


def test_schemas_use_canonical_dtypes() -> None:
    assert dict(TRADES_SCHEMA) == {
        "entry_time_ms": pl.Int64,
        "exit_time_ms": pl.Int64,
        "side": pl.Int8,
        "entry_price": pl.Float32,
        "exit_price": pl.Float32,
        "size": pl.Float32,
        "fees": pl.Float32,
        "pnl": pl.Float32,
        "return": pl.Float32,
        "exit_reason": pl.Int8,
    }
    assert dict(EQUITY_SCHEMA) == {
        "open_time_ms": pl.Int64,
        "equity": pl.Float32,
        "drawdown": pl.Float32,
    }
    assert (SIDE_LONG, EXIT_REASON_SIGNAL, EXIT_REASON_END_OF_DATA) == (1, 0, 1)


def test_accepts_valid_output() -> None:
    validate_engine_output(_Output(_trades(), _equity()), N_BARS)


def test_accepts_output_without_trades() -> None:
    validate_engine_output(_Output(_trades().clear(), _equity()), N_BARS)


def _rejects(output: _Output, match: str, n_bars: int = N_BARS) -> None:
    with pytest.raises(OutputOutOfRangeError, match=match) as info:
        validate_engine_output(output, n_bars)
    assert info.value.code == "output_out_of_range"
    assert info.value.exit_code == 3


def test_rejects_wrong_dtype() -> None:
    trades = _trades().with_columns(pl.col("entry_price").cast(pl.Float64))
    _rejects(_Output(trades, _equity()), "entry_price")


def test_rejects_wrong_time_dtype() -> None:
    equity = _equity().with_columns(pl.col("open_time_ms").cast(pl.Int32))
    _rejects(_Output(_trades(), equity), "open_time_ms")


def test_rejects_missing_or_extra_columns() -> None:
    _rejects(_Output(_trades().drop("fees"), _equity()), "columnas")
    extra = _equity().with_columns(pl.lit(1, dtype=pl.Int8).alias("position"))
    _rejects(_Output(_trades(), extra), "columnas")


def test_rejects_side_zero() -> None:
    _rejects(_Output(_trades(side=[1, 0]), _equity()), "side")


def test_rejects_exit_reason_two() -> None:
    _rejects(_Output(_trades(exit_reason=[0, 2]), _equity()), "exit_reason")


def test_rejects_nan_in_equity() -> None:
    _rejects(
        _Output(_trades(), _equity(equity=[10_000.0, float("nan"), 1.0, 1.0])), "equity: equity"
    )


def test_rejects_infinite_trade_value() -> None:
    _rejects(_Output(_trades(pnl=[1.0, float("inf")]), _equity()), "pnl: .*no finitos")


def test_rejects_null_values() -> None:
    _rejects(_Output(_trades(size=[None, 1.0]), _equity()), "size: contiene nulos")


def test_rejects_unsorted_trades() -> None:
    trades = _trades().reverse()
    _rejects(_Output(trades, _equity()), "entry_time_ms")


def test_rejects_unsorted_equity() -> None:
    equity = _equity(open_time_ms=[0, 2 * HOUR_MS, HOUR_MS, 3 * HOUR_MS])
    _rejects(_Output(_trades(), equity), "open_time_ms")


def test_rejects_positive_drawdown() -> None:
    _rejects(_Output(_trades(), _equity(drawdown=[0.0, 0.1, 0.0, 0.0])), "drawdown")


def test_rejects_exit_before_entry() -> None:
    _rejects(_Output(_trades(exit_time_ms=[HOUR_MS, HOUR_MS]), _equity()), "exit_time_ms")


@pytest.mark.parametrize("n_bars", [N_BARS - 1, N_BARS + 1])
def test_rejects_equity_height_different_from_n_bars(n_bars: int) -> None:
    _rejects(_Output(_trades(), _equity()), "n_bars", n_bars=n_bars)
