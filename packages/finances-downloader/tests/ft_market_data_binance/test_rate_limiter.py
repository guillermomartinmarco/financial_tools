"""T1.1 — TokenBucket: capacidad, recarga y bloqueo cuando está vacío."""

from __future__ import annotations

import time

from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket, TokenBucket


async def test_bucket_starts_full_and_consumes_capacity() -> None:
    bucket = TokenBucket(capacity=5, refill_rate=1.0)
    assert bucket.available_tokens() == 5
    started = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - started < 0.1
    assert bucket.available_tokens() == 0


async def test_acquire_blocks_until_refill_when_empty() -> None:
    bucket = TokenBucket(capacity=1, refill_rate=20.0)  # 1 token cada 50 ms
    await bucket.acquire()
    started = time.monotonic()
    await bucket.acquire()
    waited = time.monotonic() - started
    assert waited >= 0.04


async def test_tokens_refill_over_time_but_never_exceed_capacity() -> None:
    bucket = TokenBucket(capacity=3, refill_rate=1000.0)
    for _ in range(3):
        await bucket.acquire()
    time.sleep(0.02)
    await bucket.acquire()  # dispara la recarga
    assert bucket.available_tokens() <= 3
    await bucket.reset()
    assert bucket.available_tokens() == 3


async def test_dual_bucket_isolates_sources() -> None:
    dual = DualTokenBucket(public_capacity=1, public_refill=0.001, rest_capacity=5, rest_refill=1.0)
    await dual.acquire_public()
    assert dual.public.available_tokens() == 0
    await dual.acquire_rest(2)
    assert dual.rest.available_tokens() == 3
