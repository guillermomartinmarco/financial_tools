"""Tests de ``finances_core.timeutils.parse_time_input`` (compartido por varias herramientas)."""

from __future__ import annotations

import time

import pytest

from finances_core.timeutils import parse_time_input

JAN_1_2024_MS = 1_704_067_200_000


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
