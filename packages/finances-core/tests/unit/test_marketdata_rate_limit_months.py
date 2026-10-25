"""T1.3: ``TokenBucket`` del núcleo (Art. 14) y helpers de meses UTC."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from finances_core.marketdata.months import get_months_in_range, month_bounds, ms_to_year_month
from finances_core.marketdata.rate_limit import TokenBucket


def ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


async def test_token_bucket_burst_up_to_capacity_without_waiting() -> None:
    bucket = TokenBucket(capacity=5, refill_rate=1.0)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.1
    assert bucket.available_tokens() == 0


async def test_token_bucket_waits_for_refill() -> None:
    bucket = TokenBucket(capacity=1, refill_rate=20.0)
    await bucket.acquire()
    start = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - start >= 0.04


async def test_token_bucket_reset_restores_capacity() -> None:
    bucket = TokenBucket(capacity=3, refill_rate=0.001)
    await bucket.acquire(3)
    await bucket.reset()
    assert bucket.available_tokens() == 3


def test_ms_to_year_month() -> None:
    assert ms_to_year_month(ms(2024, 2, 29)) == (2024, 2)


def test_month_bounds_handles_december() -> None:
    assert month_bounds(2023, 12) == (ms(2023, 12, 1), ms(2024, 1, 1))
    assert month_bounds(2024, 2) == (ms(2024, 2, 1), ms(2024, 3, 1))


def test_get_months_in_range_crosses_year() -> None:
    assert get_months_in_range(ms(2023, 11, 15), ms(2024, 2, 1)) == [
        (2023, 11),
        (2023, 12),
        (2024, 1),
        (2024, 2),
    ]
    assert get_months_in_range(ms(2024, 5, 3), ms(2024, 5, 4)) == [(2024, 5)]
