"""T2.5 — Planificación del fallback REST paginado."""

from __future__ import annotations

from finances_core.config import FinancesSettings

from finances_downloader.ft_market_data_binance.client import BinanceRestClient
from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket
from finances_downloader.ft_market_data_binance.rest_api import plan_rest_fallback

from .conftest import ms

H = 3_600_000


def _client(settings: FinancesSettings) -> BinanceRestClient:
    return BinanceRestClient(DualTokenBucket(1, 1.0, 1, 1.0), settings=settings)


def test_5000_candles_with_limit_1000_gives_5_items(settings: FinancesSettings) -> None:
    start = ms(2024, 1, 1)
    gap = (start, start + 5000 * H)
    items = plan_rest_fallback([gap], "BTCUSDT", "1h", "spot", 1000, _client(settings))
    assert len(items) == 5
    for i, item in enumerate(items):
        lo = start + i * 1000 * H
        hi = lo + 1000 * H
        assert item.source == "rest_api"
        assert item.endpoint == "/api/v3/klines"
        assert item.covers_range_ms == [lo, hi]
        assert item.params == {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "startTime": lo,
            "endTime": hi,
            "limit": 1000,
        }


def test_small_gap_gives_single_item(settings: FinancesSettings) -> None:
    start = ms(2024, 1, 1)
    items = plan_rest_fallback(
        [(start, start + 10 * H)], "ETHUSDT", "1h", "futures", 1000, _client(settings)
    )
    assert len(items) == 1
    assert items[0].endpoint == "/fapi/v1/klines"
    assert items[0].covers_range_ms == [start, start + 10 * H]


def test_no_gaps_no_items(settings: FinancesSettings) -> None:
    assert plan_rest_fallback([], "BTCUSDT", "1h", "spot", 1000, _client(settings)) == []
