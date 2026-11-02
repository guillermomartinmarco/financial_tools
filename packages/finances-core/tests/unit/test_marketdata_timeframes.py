"""Grilla de timeframes: alineación de ``open_time_ms`` al inicio de una vela (RF-5)."""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from finances_core.marketdata.timeframes import (
    aligned_expr,
    is_aligned,
    timeframe_duration_ms,
)

HOUR = 3_600_000
DAY = 86_400_000


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [("1m", 60_000), ("1h", HOUR), ("4h", 4 * HOUR), ("1d", DAY), ("3d", 3 * DAY)],
)
def test_fixed_durations_reuse_core_map(timeframe: str, expected: int) -> None:
    assert timeframe_duration_ms(timeframe) == expected


def test_unknown_timeframe_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeframe desconocido"):
        timeframe_duration_ms("7x")
    with pytest.raises(ValueError, match="timeframe desconocido"):
        is_aligned(0, "7x")


@pytest.mark.parametrize(
    ("timeframe", "ts", "expected"),
    [
        ("1h", _ms(2024, 1, 1, 5), True),
        ("1h", _ms(2024, 1, 1, 5, 30), False),
        ("1h", _ms(2024, 1, 1, 5) + 1, False),
        ("4h", _ms(2024, 1, 1, 4), True),
        ("4h", _ms(2024, 1, 1, 3), False),
        ("1m", _ms(2024, 1, 1, 0, 7), True),
        ("1d", _ms(2024, 1, 1), True),
        ("1d", _ms(2024, 1, 1, 3), False),  # +3 h: zona horaria equivocada
        ("1w", _ms(2024, 1, 1), True),  # 2024-01-01 fue lunes
        ("1w", _ms(2024, 1, 8), True),
        ("1w", _ms(2024, 1, 2), False),  # martes
        ("1w", _ms(2023, 12, 31), False),  # domingo
        ("1w", _ms(2024, 1, 1, 1), False),  # lunes 01:00
        ("1M", _ms(2024, 1, 1), True),
        ("1M", _ms(2024, 2, 1), True),
        ("1M", _ms(2024, 2, 2), False),
        ("1M", _ms(2024, 2, 1, 2), False),
    ],
)
def test_is_aligned(timeframe: str, ts: int, expected: bool) -> None:
    assert is_aligned(ts, timeframe) is expected


@pytest.mark.parametrize("timeframe", ["1m", "1h", "1d", "3d", "1w", "1M"])
def test_expression_matches_scalar(timeframe: str) -> None:
    samples = [
        _ms(2024, 1, 1),
        _ms(2024, 1, 1, 3),
        _ms(2024, 1, 2),
        _ms(2024, 2, 1),
        _ms(2024, 2, 1, 0, 1),
        _ms(2024, 1, 8),
        _ms(2023, 12, 31),
        _ms(2024, 1, 4),
    ]
    out = pl.DataFrame({"t": samples}, schema={"t": pl.Int64}).select(
        aligned_expr(pl.col("t"), timeframe).alias("ok")
    )["ok"]
    assert out.to_list() == [is_aligned(s, timeframe) for s in samples]


def test_expression_unknown_timeframe() -> None:
    with pytest.raises(ValueError, match="timeframe desconocido"):
        aligned_expr(pl.col("t"), "nope")
