"""T2.4 — Disponibilidad en Public Data: cada mes ausente cae a REST (RF-1 pasos 4-5)."""

from __future__ import annotations

from finances_core.config import FinancesSettings

from finances_downloader.ft_market_data_binance.client import BinancePublicDataClient
from finances_downloader.ft_market_data_binance.public_data import (
    check_public_data_availability,
    get_months_in_range,
    month_bounds,
)
from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket

from .conftest import FakeBinance, ms


def _client(
    settings: FinancesSettings, fake: FakeBinance, limiter: DualTokenBucket | None = None
) -> BinancePublicDataClient:
    return BinancePublicDataClient(
        limiter or DualTokenBucket(1000, 1000.0, 1000, 1000.0),
        settings=settings,
        transport=fake.transport,
    )


def test_months_in_range_crosses_year() -> None:
    assert get_months_in_range(ms(2023, 11, 15), ms(2024, 2, 3)) == [
        (2023, 11),
        (2023, 12),
        (2024, 1),
        (2024, 2),
    ]


def test_month_bounds_december() -> None:
    assert month_bounds(2023, 12) == (ms(2023, 12), ms(2024, 1))


async def test_existing_months_become_public_items(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 2)
    gap = (ms(2024, 1, 10), ms(2024, 2, 20))
    async with _client(settings, fake_binance) as client:
        items, uncovered = await check_public_data_availability(
            [gap], "BTCUSDT", "1h", "spot", client
        )
    assert uncovered == []
    assert [i.url for i in items] == [
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip",
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-02.zip",
    ]
    assert items[0].covers_range_ms == [ms(2024, 1, 10), ms(2024, 2, 1)]
    assert items[1].covers_range_ms == [ms(2024, 2, 1), ms(2024, 2, 20)]
    assert all(i.source == "public_data" for i in items)
    assert all(i.expected_checksum and i.expected_checksum.startswith("sha256:") for i in items)


async def test_each_missing_month_goes_to_rest(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    """Bug #2: antes bastaba UN mes existente para dar el gap entero por cubierto."""
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 2)  # solo febrero existe
    gap = (ms(2024, 1, 10), ms(2024, 4, 5))
    async with _client(settings, fake_binance) as client:
        items, uncovered = await check_public_data_availability(
            [gap], "BTCUSDT", "1h", "spot", client
        )
    assert [i.covers_range_ms for i in items] == [[ms(2024, 2, 1), ms(2024, 3, 1)]]
    assert uncovered == [(ms(2024, 1, 10), ms(2024, 2, 1)), (ms(2024, 3, 1), ms(2024, 4, 5))]


async def test_contiguous_missing_months_are_merged(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    gap = (ms(2024, 1, 10), ms(2024, 3, 5))
    async with _client(settings, fake_binance) as client:
        items, uncovered = await check_public_data_availability(
            [gap], "BTCUSDT", "1h", "spot", client
        )
    assert items == []
    assert uncovered == [gap]


async def test_availability_respects_rate_limit(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    limiter = DualTokenBucket(
        public_capacity=100, public_refill=0.0001, rest_capacity=1, rest_refill=1
    )
    gap = (ms(2024, 1, 10), ms(2024, 3, 5))  # 3 meses inexistentes -> 3 HEAD
    async with _client(settings, fake_binance, limiter) as client:
        await check_public_data_availability([gap], "BTCUSDT", "1h", "spot", client)
    assert limiter.public.available_tokens() == 97
