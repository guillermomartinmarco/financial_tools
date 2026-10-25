"""T2.1 / T2.6 / RF-1 paso 1 — validación y generación del plan de descarga."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data import DownloadPlan, DownloadRequest
from finances_core.timeutils import parse_time_input
from pydantic import ValidationError

from finances_downloader.ft_market_data_binance import planner
from finances_downloader.ft_market_data_binance.normalizer import normalize_rest_json
from finances_downloader.ft_market_data_binance.planner import (
    FUTURE_DATA_MESSAGE,
    build_download_plan,
    generate_download_plan,
    load_plan,
    plan_file_path,
    validate_request,
)
from finances_downloader.ft_market_data_binance.writer import write_parquet_partitioned

from .conftest import FakeBinance, kline_row, ms

H = 3_600_000
D = 24 * H


def _request(**overrides: object) -> DownloadRequest:
    data: dict[str, object] = {
        "symbols": ["BTCUSDT"],
        "timeframes": ["1h"],
        "start_ms": ms(2024, 1, 1),
        "end_ms": ms(2024, 3, 15),
        "market_type": "spot",
    }
    data.update(overrides)
    return DownloadRequest.model_validate(data)


# ------------------------------------------------------------------- T2.1
def test_parse_time_input_comes_from_finances_core() -> None:
    assert not hasattr(planner, "parse_time_input")
    assert parse_time_input("2024-01-01") == 1704067200000
    assert parse_time_input("1704067200000") == 1704067200000
    assert parse_time_input("2024-01-01T01:00:00+01:00") == 1704067200000
    assert abs(parse_time_input("now") - int(time.time() * 1000)) < 5000


# --------------------------------------------------------------- validación
def test_future_start_is_rejected() -> None:
    now = ms(2024, 6, 1)
    with pytest.raises(ValueError, match=FUTURE_DATA_MESSAGE):
        validate_request(_request(start_ms=now + H, end_ms=None), now_ms=now)


def test_start_within_last_candle_is_rejected_per_timeframe() -> None:
    """RF-1: ``start_ms`` DEBE ser ≤ ``now_ms - 1 vela`` del timeframe pedido."""
    now = ms(2024, 6, 1)
    req = _request(start_ms=now - 2 * H, end_ms=None, timeframes=["1h", "1d"])
    with pytest.raises(ValueError, match=FUTURE_DATA_MESSAGE):
        validate_request(req, now_ms=now)
    assert validate_request(_request(start_ms=now - H, end_ms=None), now_ms=now) == now


def test_end_in_future_is_clamped_to_now() -> None:
    now = ms(2024, 6, 1)
    assert validate_request(_request(start_ms=ms(2024, 1, 1), end_ms=now + D), now_ms=now) == now


def test_invalid_timeframe_and_range_rejected_by_model() -> None:
    with pytest.raises(ValidationError):
        _request(timeframes=["7h"])
    with pytest.raises(ValidationError):
        _request(start_ms=ms(2024, 2, 1), end_ms=ms(2024, 1, 1))


# ------------------------------------------------------------------- T2.6
async def test_generate_plan_end_to_end(
    settings: FinancesSettings, fake_binance: FakeBinance, tmp_path: Path
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 2)
    # Datos locales: 1-ene .. 10-ene completos
    rows = [kline_row(ms(2024, 1, 1) + i * H) for i in range(10 * 24)]
    write_parquet_partitioned(
        normalize_rest_json(rows, "BTCUSDT", "1h", "spot", 0, 2**62), settings.PARQUET_DIR
    )

    plan = await generate_download_plan(
        _request(), settings=settings, transport=fake_binance.transport
    )

    assert uuid.UUID(plan.task_id).version == 7
    public = [i for i in plan.items if i.source == "public_data"]
    rest = [i for i in plan.items if i.source == "rest_api"]
    assert [i.covers_range_ms for i in public] == [
        [ms(2024, 1, 11), ms(2024, 2, 1)],
        [ms(2024, 2, 1), ms(2024, 3, 1)],
    ]
    assert [i.covers_range_ms for i in rest] == [[ms(2024, 3, 1), ms(2024, 3, 15)]]
    assert rest[0].params is not None
    assert rest[0].params["startTime"] == ms(2024, 3, 1)

    path = plan_file_path(plan.task_id)
    assert path.parent == settings.SHARE_DIR / "plans"
    assert path.parent.parent == settings.TASKS_DB_PATH.parent
    assert DownloadPlan.model_validate(json.loads(path.read_text())) == plan
    assert load_plan(plan.task_id) == plan
    assert load_plan("00000000-0000-0000-0000-000000000000") is None


async def test_plan_is_empty_when_local_data_covers_request(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    start = ms(2024, 1, 1)
    rows = [kline_row(start + i * H) for i in range(48)]
    write_parquet_partitioned(
        normalize_rest_json(rows, "BTCUSDT", "1h", "spot", 0, 2**62), settings.PARQUET_DIR
    )
    outcome = await build_download_plan(
        _request(start_ms=start, end_ms=start + 47 * H),
        settings=settings,
        transport=fake_binance.transport,
    )
    assert outcome.plan.items == []
    assert outcome.gaps_found == 0


async def test_month_boundary_between_local_partitions_is_not_a_gap(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    start = ms(2024, 1, 31, 20)
    rows = [kline_row(start + i * H) for i in range(10)]  # cruza a febrero
    write_parquet_partitioned(
        normalize_rest_json(rows, "BTCUSDT", "1h", "spot", 0, 2**62), settings.PARQUET_DIR
    )
    outcome = await build_download_plan(
        _request(start_ms=start, end_ms=start + 9 * H),
        settings=settings,
        transport=fake_binance.transport,
    )
    assert outcome.plan.items == []


async def test_unknown_symbol_is_skipped_with_warning(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    outcome = await build_download_plan(
        _request(symbols=["BTCUSDT", "FAKEUSDT"], start_ms=ms(2024, 3, 1)),
        settings=settings,
        transport=fake_binance.transport,
    )
    assert outcome.skipped_symbols == ["FAKEUSDT"]
    assert {i.symbol for i in outcome.plan.items} == {"BTCUSDT"}


async def test_futures_plan_uses_futures_sources(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.add_month("futures", "ETHUSDT", "1h", 2024, 1, header=True)
    plan = await generate_download_plan(
        _request(symbols=["ETHUSDT"], market_type="futures", end_ms=ms(2024, 2, 2)),
        settings=settings,
        transport=fake_binance.transport,
    )
    assert plan.items[0].url is not None
    assert "/data/futures/um/monthly/klines/ETHUSDT/1h/" in plan.items[0].url
    assert plan.items[-1].endpoint == "/fapi/v1/klines"
