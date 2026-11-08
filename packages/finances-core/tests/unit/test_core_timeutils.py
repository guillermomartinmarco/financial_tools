"""Tests de ``finances_core.timeutils.parse_time_input`` (compartido por varias herramientas)."""

from __future__ import annotations

import time

import pytest

from finances_core.timeutils import (
    format_table_cell,
    format_time_ms,
    humanize_ms_header,
    parse_time_input,
)

JAN_1_2024_MS = 1_704_067_200_000
# 2024-01-07 00:00:00 UTC == JAN_1_2024_MS + 6 días = 1_704_585_600_000
JAN_7_2024_MS = 1_704_585_600_000


@pytest.mark.parametrize(
    "raw",
    [
        "2024-01-01",
        "2024-01-01T00:00:00Z",
        "2024-01-01T00:00:00+00:00",
        "2024/01/01",
        str(JAN_1_2024_MS),
    ],
)
def test_parse_known_formats(raw: str) -> None:
    assert parse_time_input(raw) == JAN_1_2024_MS


def test_parse_now_is_current_time() -> None:
    before = int(time.time() * 1000)
    parsed = parse_time_input("now")
    assert before <= parsed <= int(time.time() * 1000)


def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError, match="no reconocido"):
        parse_time_input("ayer")


def test_format_time_ms_known_value() -> None:
    assert format_time_ms(JAN_7_2024_MS) == "2024-01-07 00:00:00"


def test_format_time_ms_none_is_dash() -> None:
    assert format_time_ms(None) == "-"


def test_format_table_cell_timestamp_column() -> None:
    assert format_table_cell("entry_time_ms", JAN_7_2024_MS) == "2024-01-07 00:00:00"


def test_format_table_cell_non_timestamp_ms_stays_raw() -> None:
    # ``duration_ms`` es una duración, no un timestamp: debe quedar en crudo.
    assert format_table_cell("duration_ms", 3_600_000) == "3600000"


def test_format_table_cell_non_int_value_stays_str() -> None:
    assert format_table_cell("entry_time_ms", None) == "None"
    assert format_table_cell("symbol", "BTCUSDT") == "BTCUSDT"


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("entry_time_ms", "entry_time"),
        ("open_time_ms", "open_time"),
        ("start_ms", "start"),
        ("stable_from_ms", "stable_from"),
        ("duration_ms", "duration_ms"),
        ("symbol", "symbol"),
    ],
)
def test_humanize_ms_header(column: str, expected: str) -> None:
    assert humanize_ms_header(column) == expected
