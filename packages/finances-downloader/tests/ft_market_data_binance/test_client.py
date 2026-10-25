"""T1.2-T1.4 — Clientes HTTP (Public Data + REST), reintentos y checksums."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from finances_core.config import FinancesSettings
from structlog.testing import capture_logs

from finances_downloader.ft_market_data_binance.client import (
    BinancePublicDataClient,
    BinanceRestClient,
    ChecksumMismatchError,
    PublicDataNotFoundError,
    build_wait,
    checksum_sha256,
    checksums_match,
    parse_checksum_file,
)
from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket

from .conftest import FakeBinance, ms

SPOT_URL = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"


def _limiter() -> DualTokenBucket:
    return DualTokenBucket(1000, 1000.0, 1000, 1000.0)


# --------------------------------------------------------------------- Public Data
def test_build_monthly_url_spot_and_futures(settings: FinancesSettings) -> None:
    client = BinancePublicDataClient(_limiter(), settings=settings)
    assert client.build_monthly_url("btcusdt", "1h", "spot", 2024, 1) == SPOT_URL
    assert client.build_monthly_url("ETHUSDT", "5m", "futures", 2023, 11) == (
        "https://data.binance.vision/data/futures/um/monthly/klines/ETHUSDT/5m/"
        "ETHUSDT-5m-2023-11.zip"
    )


async def test_check_exists_200_returns_prefixed_checksum(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    payload = fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    async with BinancePublicDataClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        exists, checksum = await client.check_exists("BTCUSDT", "1h", "spot", 2024, 1)
    assert exists is True
    assert checksum == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    assert fake_binance.count("HEAD", "BTCUSDT-1h-2024-01.zip") == 1


async def test_check_exists_404_returns_false(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    async with BinancePublicDataClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        assert await client.check_exists("BTCUSDT", "1h", "spot", 2024, 2) == (False, None)


async def test_check_exists_without_checksum_file(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    fake_binance.publish_checksums = False
    async with BinancePublicDataClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        assert await client.check_exists("BTCUSDT", "1h", "spot", 2024, 1) == (True, None)


async def test_download_streams_to_temp_file_and_validates_checksum(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    payload = fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    expected = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    async with BinancePublicDataClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        path = await client.download(SPOT_URL, expected)
    try:
        assert path.read_bytes() == payload
    finally:
        path.unlink()


async def test_download_checksum_mismatch_raises_and_cleans_temp(
    settings: FinancesSettings, fake_binance: FakeBinance, tmp_path: Path
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    async with BinancePublicDataClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        with pytest.raises(ChecksumMismatchError):
            await client.download(SPOT_URL, "sha256:" + "0" * 64)
    assert not list((tmp_path / "systemp").glob("*.zip"))


async def test_download_404_raises_not_found(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    async with BinancePublicDataClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        with pytest.raises(PublicDataNotFoundError):
            await client.download(SPOT_URL, None)


async def test_public_download_retries_on_5xx(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    payload = fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    fake_binance.forced_status["/data/spot/monthly"] = [503, 502]
    async with BinancePublicDataClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        path = await client.download(SPOT_URL, None)
    assert path.read_bytes() == payload
    path.unlink()
    assert fake_binance.count("GET", "BTCUSDT-1h-2024-01.zip") == 3


async def test_public_requests_respect_rate_limiter(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    limiter = DualTokenBucket(
        public_capacity=3, public_refill=0.0001, rest_capacity=5, rest_refill=1
    )
    async with BinancePublicDataClient(
        limiter, settings=settings, transport=fake_binance.transport
    ) as client:
        await client.check_exists("BTCUSDT", "1h", "spot", 2024, 1)
    assert limiter.public.available_tokens() == 2
    assert limiter.rest.available_tokens() == 5


# ----------------------------------------------------------------------- REST
async def test_fetch_klines_paginates(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    start = ms(2024, 1, 1)
    end = start + 24 * 3_600_000 - 1  # 24 velas de 1h
    async with BinanceRestClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        rows = await client.fetch_klines("BTCUSDT", "1h", "spot", start, end, limit=10)
    assert [r[0] for r in rows] == [start + i * 3_600_000 for i in range(24)]
    assert fake_binance.count("GET", "/api/v3/klines") == 3
    first = fake_binance.requests[0].url.params
    assert first["startTime"] == str(start)
    assert first["limit"] == "10"


async def test_fetch_klines_futures_endpoint(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    start = ms(2024, 1, 1)
    async with BinanceRestClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        rows = await client.fetch_klines("ETHUSDT", "1h", "futures", start, start + 3_600_000)
    assert len(rows) == 2
    assert fake_binance.requests[0].url.host == "fapi.binance.com"
    assert fake_binance.requests[0].url.path == "/fapi/v1/klines"


@pytest.mark.parametrize("status", [429, 418, 500, 503])
async def test_rest_retries_on_rate_limit_and_5xx(
    settings: FinancesSettings, fake_binance: FakeBinance, status: int
) -> None:
    fake_binance.forced_status["/api/v3/klines"] = [status, status]
    start = ms(2024, 1, 1)
    with capture_logs() as logs:
        async with BinanceRestClient(
            _limiter(), settings=settings, transport=fake_binance.transport
        ) as client:
            rows = await client.fetch_klines("BTCUSDT", "1h", "spot", start, start)
    assert len(rows) == 1
    assert fake_binance.count("GET", "/api/v3/klines") == 3
    retries = [e for e in logs if e["event"] == "binance_http_retry"]
    assert len(retries) == 2
    assert retries[0]["status_code"] == status
    assert retries[0]["attempt"] == 1


async def test_rest_retries_on_network_error(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.forced_errors["/api/v3/klines"] = [httpx.ConnectError("boom")]
    start = ms(2024, 1, 1)
    async with BinanceRestClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        rows = await client.fetch_klines("BTCUSDT", "1h", "spot", start, start)
    assert len(rows) == 1


async def test_rest_does_not_retry_on_4xx(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    start = ms(2024, 1, 1)
    async with BinanceRestClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_klines("NOPEUSDT", "1h", "spot", start, start)
    assert fake_binance.count("GET", "/api/v3/klines") == 1


async def test_rest_exhausts_max_retries_then_raises(
    monkeypatch: pytest.MonkeyPatch, fake_binance: FakeBinance
) -> None:
    monkeypatch.setenv("FT_BINANCE_MAX_RETRIES", "2")
    monkeypatch.setenv("FT_BINANCE_BASE_BACKOFF_MS", "0")
    settings = FinancesSettings()
    fake_binance.forced_status["/api/v3/klines"] = [429] * 10
    start = ms(2024, 1, 1)
    async with BinanceRestClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_klines("BTCUSDT", "1h", "spot", start, start)
    # 1 intento + FT_BINANCE_MAX_RETRIES reintentos
    assert fake_binance.count("GET", "/api/v3/klines") == 3


def test_backoff_is_exponential_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_BINANCE_BASE_BACKOFF_MS", "1000")
    wait = build_wait(FinancesSettings(), jitter=False)

    class _State:
        def __init__(self, n: int) -> None:
            self.attempt_number = n

    waits = [wait(_State(n)) for n in (1, 2, 3)]
    assert waits == [1.0, 2.0, 4.0]


async def test_fetch_symbols_from_exchange_info(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    async with BinanceRestClient(
        _limiter(), settings=settings, transport=fake_binance.transport
    ) as client:
        symbols = await client.fetch_symbols("spot")
    assert symbols == {"BTCUSDT", "ETHUSDT"}
    assert fake_binance.requests[0].url.path == "/api/v3/exchangeInfo"


# ------------------------------------------------------------------- checksum
def test_checksum_sha256_known_content(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    assert checksum_sha256(f) == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_parse_checksum_file_binance_format() -> None:
    digest = "a" * 64
    assert parse_checksum_file(f"{digest}  BTCUSDT-1h-2024-01.zip\n") == f"sha256:{digest}"
    assert parse_checksum_file(digest.upper()) == f"sha256:{digest}"
    with pytest.raises(ValueError, match="CHECKSUM"):
        parse_checksum_file("not-a-checksum")


def test_checksums_match_ignores_prefix_and_case() -> None:
    digest = "b" * 64
    assert checksums_match(f"sha256:{digest}", digest.upper())
    assert not checksums_match(f"sha256:{digest}", "c" * 64)
