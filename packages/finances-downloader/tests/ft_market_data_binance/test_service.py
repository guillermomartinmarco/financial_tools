"""T4.1-T4.3 / RF-2 / RF-6 / RF-9 — orquestador, ciclo de vida de la tarea y errores."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data import DownloadPlan, DownloadPlanItem, DownloadRequest
from finances_core.tasks import SQLTaskManager
from structlog.testing import capture_logs

from finances_downloader.ft_market_data_binance import service
from finances_downloader.ft_market_data_binance.service import (
    PHASE_ORDER,
    TaskNotFinishedError,
    create_download_task,
    execute_download_plan,
    get_progress,
    get_result,
    new_task_id,
    resolve_max_concurrent,
    run_download,
)

from .conftest import FakeBinance, ms

H = 3_600_000
PUB = (
    "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-{y:04d}-{m:02d}.zip"
)


def _pub(y: int, m: int, lo: int, hi: int, checksum: str | None = None) -> DownloadPlanItem:
    return DownloadPlanItem(
        source="public_data",
        symbol="BTCUSDT",
        timeframe="1h",
        market_type="spot",
        url=PUB.format(y=y, m=m),
        expected_checksum=checksum,
        covers_range_ms=[lo, hi],
    )


def _rest(lo: int, hi: int, symbol: str = "BTCUSDT") -> DownloadPlanItem:
    return DownloadPlanItem(
        source="rest_api",
        symbol=symbol,
        timeframe="1h",
        market_type="spot",
        endpoint="/api/v3/klines",
        params={"symbol": symbol, "interval": "1h", "startTime": lo, "endTime": hi, "limit": 1000},
        covers_range_ms=[lo, hi],
    )


def _plan(*items: DownloadPlanItem) -> DownloadPlan:
    return DownloadPlan(task_id=new_task_id(), created_at_ms=1, items=list(items))


def _statuses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = SQLTaskManager.update_task

    def spy(self: SQLTaskManager, task_id: str, **fields: object) -> None:
        status = fields.get("status")
        if isinstance(status, str) and (not seen or seen[-1] != status):
            seen.append(status)
        real(self, task_id, **fields)

    monkeypatch.setattr(SQLTaskManager, "update_task", spy)
    return seen


# --------------------------------------------------------------------- T4.1
async def test_execute_plan_public_and_rest(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    plan = _plan(
        _pub(2024, 1, ms(2024, 1, 1), ms(2024, 2, 1)),
        _rest(ms(2024, 2, 1), ms(2024, 2, 2)),
    )
    result = await execute_download_plan(
        plan, 2, settings=settings, transport=fake_binance.transport
    )
    assert result.status == "COMPLETED"
    assert len(result.parquet_paths) == 2
    stats = result.stats["BTCUSDT/1h/spot"]
    assert isinstance(stats, dict)
    assert stats["public"] == 1
    assert stats["rest"] == 1
    assert stats["candles"] == 31 * 24 + 25
    jan = pl.read_parquet(result.parquet_paths[0])
    assert jan.height == 31 * 24


async def test_semaphore_limits_concurrent_downloads(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return fake_binance.handle(request)

    start = ms(2024, 1, 1)
    plan = _plan(*[_rest(start + i * 10 * H, start + (i * 10 + 9) * H) for i in range(6)])
    result = await execute_download_plan(
        plan, 2, settings=settings, transport=httpx.MockTransport(handler)
    )
    assert result.status == "COMPLETED"
    assert peak == 2


# --------------------------------------------------------------------- T4.2
async def test_phases_are_persisted_in_monotonic_order(
    settings: FinancesSettings, fake_binance: FakeBinance, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    seen = _statuses(monkeypatch)
    request = DownloadRequest.model_validate(
        {
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h"],
            "start_ms": ms(2024, 1, 20),
            "end_ms": ms(2024, 2, 3),
        }
    )
    task_id = new_task_id()
    create_download_task(task_id, request, settings=settings)
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(task_id)
    assert row is not None
    assert row["status"] == "PENDING"

    result = await run_download(
        request, task_id, settings=settings, transport=fake_binance.transport
    )

    assert result.status == "COMPLETED"
    assert seen == [
        "PLANNING",
        "DOWNLOADING_PUBLIC",
        "DOWNLOADING_REST",
        "NORMALIZING",
        "WRITING_PARQUET",
        "COMPLETED",
    ]
    assert [PHASE_ORDER.index(s) for s in seen] == sorted(PHASE_ORDER.index(s) for s in seen)
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(task_id)
    assert row is not None
    assert row["status"] == "COMPLETED"
    assert row["downloaded_bytes"] > 0
    assert row["processed_candles"] == (12 * 24) + (2 * 24 + 1)
    assert row["gaps_found"] == 1
    assert row["gaps_filled_public"] == 1
    assert row["gaps_filled_rest"] == 1
    assert json.loads(row["errors_json"]) == []
    assert row["finished_at_ms"] is not None


async def test_progress_has_dto_shape(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    request = DownloadRequest.model_validate(
        {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "timeframes": ["1h", "4h"],
            "start_ms": ms(2024, 3, 1),
            "end_ms": ms(2024, 3, 2),
        }
    )
    task_id = new_task_id()
    create_download_task(task_id, request, settings=settings)
    pending = get_progress(task_id, settings=settings)
    assert pending is not None
    assert pending.status == "PENDING"
    assert (pending.symbols_total, pending.timeframes_total) == (2, 2)
    assert (pending.symbols_done, pending.timeframes_done) == (0, 0)

    await run_download(request, task_id, settings=settings, transport=fake_binance.transport)
    done = get_progress(task_id, settings=settings)
    assert done is not None
    assert done.status == "COMPLETED"
    assert (done.symbols_done, done.timeframes_done) == (2, 2)
    assert done.processed_candles == 2 * (25 + 7)
    assert get_progress("missing", settings=settings) is None


# --------------------------------------------------------------------- T4.3
async def test_failed_item_does_not_stop_others(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    """Public Data 404 + REST sin datos -> gap no rellenable; los otros 3 items terminan."""
    start = ms(2024, 2, 1)
    fake_binance.rest_from_ms = start + 1  # REST no tiene enero (primera vela: 01:00)
    plan = _plan(
        _pub(2024, 1, ms(2024, 1, 1), ms(2024, 2, 1)),  # no publicado -> 404
        _rest(start, start + 5 * H),
        _rest(start + 10 * H, start + 15 * H),
        _rest(start + 20 * H, start + 25 * H),
    )
    task_id = plan.task_id
    with capture_logs() as logs:
        result = await execute_download_plan(
            plan, 4, settings=settings, transport=fake_binance.transport
        )
    assert result.status == "COMPLETED"
    assert len(result.gaps_unfillable) == 1
    gap = result.gaps_unfillable[0]
    assert gap["symbol"] == "BTCUSDT"
    assert gap["range_ms"] == [ms(2024, 1, 1), ms(2024, 2, 1)]
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(task_id)
    assert row is not None
    assert len(json.loads(row["errors_json"])) == 1
    assert row["gaps_filled_rest"] == 3
    assert any(e["event"] == "gap_unfillable" and e["log_level"] == "warning" for e in logs)


async def test_network_exhausted_marks_failed_but_keeps_written_data(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    start = ms(2024, 2, 1)
    fake_binance.forced_status["/api/v3/klines"] = [503] * 4  # agota 1 + 3 reintentos
    plan = _plan(_rest(start, start + 5 * H), _rest(start + 10 * H, start + 12 * H))
    # Fuerza orden determinista: un solo slot.
    result = await execute_download_plan(
        plan, 1, settings=settings, transport=fake_binance.transport
    )
    assert result.status == "FAILED"
    assert len(result.parquet_paths) == 1
    assert pl.read_parquet(result.parquet_paths[0]).height == 3
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(plan.task_id)
    assert row is not None
    assert row["status"] == "FAILED"
    errors = json.loads(row["errors_json"])
    assert len(errors) == 1
    assert "503" in errors[0]


async def test_checksum_mismatch_retries_once_then_falls_back_to_rest(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    plan = _plan(_pub(2024, 1, ms(2024, 1, 1), ms(2024, 1, 2), checksum="sha256:" + "0" * 64))
    result = await execute_download_plan(
        plan, 2, settings=settings, transport=fake_binance.transport
    )
    assert result.status == "COMPLETED"
    assert fake_binance.count("GET", "BTCUSDT-1h-2024-01.zip") == 2
    assert fake_binance.count("GET", "/api/v3/klines") >= 1
    stats = result.stats["BTCUSDT/1h/spot"]
    assert isinstance(stats, dict)
    assert stats["rest"] == 1
    assert stats["public"] == 0
    df = pl.read_parquet(result.parquet_paths[0])
    assert df.height == 25
    assert df["open"][0] == pytest.approx(200.0)  # precio del fake REST


async def test_planning_failure_marks_task_failed(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.forced_status["/api/v3/exchangeInfo"] = [500] * 10
    request = DownloadRequest.model_validate(
        {
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h"],
            "start_ms": ms(2024, 3, 1),
            "end_ms": ms(2024, 3, 2),
        }
    )
    task_id = new_task_id()
    create_download_task(task_id, request, settings=settings)
    result = await run_download(
        request, task_id, settings=settings, transport=fake_binance.transport
    )
    assert result.status == "FAILED"
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(task_id)
    assert row is not None
    assert row["status"] == "FAILED"
    assert "500" in json.loads(row["errors_json"])[0]


async def test_unknown_symbol_recorded_and_task_completes(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    request = DownloadRequest.model_validate(
        {
            "symbols": ["BTCUSDT", "FAKEUSDT"],
            "timeframes": ["1h"],
            "start_ms": ms(2024, 3, 1),
            "end_ms": ms(2024, 3, 2),
        }
    )
    task_id = new_task_id()
    result = await run_download(
        request, task_id, settings=settings, transport=fake_binance.transport
    )
    assert result.status == "COMPLETED"
    progress = get_progress(task_id, settings=settings)
    assert progress is not None
    assert any("FAKEUSDT" in e for e in progress.errors)


async def test_rerun_is_idempotent_end_to_end(
    settings: FinancesSettings, fake_binance: FakeBinance
) -> None:
    fake_binance.add_month("spot", "BTCUSDT", "1h", 2024, 1)
    request = DownloadRequest.model_validate(
        {
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h"],
            "start_ms": ms(2024, 1, 1),
            "end_ms": ms(2024, 2, 1, 5),
        }
    )
    first = await run_download(
        request, new_task_id(), settings=settings, transport=fake_binance.transport
    )
    rows = sum(pl.read_parquet(p).height for p in first.parquet_paths)
    calls = len(fake_binance.requests)
    second = await run_download(
        request, new_task_id(), settings=settings, transport=fake_binance.transport
    )
    assert second.status == "COMPLETED"
    assert second.parquet_paths == []
    assert sum(pl.read_parquet(p).height for p in first.parquet_paths) == rows == 31 * 24 + 6
    # Segunda ejecución: sólo exchangeInfo, ninguna descarga
    new_paths = [r.url.path for r in fake_binance.requests[calls:]]
    assert new_paths == ["/api/v3/exchangeInfo"]


# ------------------------------------------------------------------ result
async def test_get_result_lifecycle(settings: FinancesSettings, fake_binance: FakeBinance) -> None:
    fake_binance.rest_from_ms = ms(2024, 3, 1, 12)
    request = DownloadRequest.model_validate(
        {
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h"],
            "start_ms": ms(2024, 2, 1),
            "end_ms": ms(2024, 2, 2),
        }
    )
    task_id = new_task_id()
    assert get_result(task_id, settings=settings) is None
    create_download_task(task_id, request, settings=settings)
    with pytest.raises(TaskNotFinishedError):
        get_result(task_id, settings=settings)
    await run_download(request, task_id, settings=settings, transport=fake_binance.transport)
    result = get_result(task_id, settings=settings)
    assert result is not None
    assert result.status == "COMPLETED"
    assert result.gaps_unfillable[0]["range_ms"] == [ms(2024, 2, 1), ms(2024, 2, 2)]


# -------------------------------------------------------------------- RF-2
def test_resolve_max_concurrent_default_creates_env_and_logs(
    monkeypatch: pytest.MonkeyPatch, settings: FinancesSettings
) -> None:
    monkeypatch.delenv("FT_DOWNLOAD_MAX_CONCURRENT", raising=False)
    with capture_logs() as logs:
        assert resolve_max_concurrent(None, settings) == 5
    assert os.environ["FT_DOWNLOAD_MAX_CONCURRENT"] == "5"
    assert any(e["event"] == "Usando default FT_DOWNLOAD_MAX_CONCURRENT=5" for e in logs)


def test_resolve_max_concurrent_override_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_DOWNLOAD_MAX_CONCURRENT", "7")
    s = FinancesSettings()
    with capture_logs() as logs:
        assert resolve_max_concurrent(None, s) == 7
    assert logs == []
    assert resolve_max_concurrent(3, s) == 3


def test_service_module_has_no_blocking_settings_singleton() -> None:
    assert not hasattr(service, "settings")
