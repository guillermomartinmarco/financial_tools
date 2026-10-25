"""T4.1 — Orquestador: fases RF-8, concurrencia, TokenBucket y reintentos RF-7."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data_yfinance import YFDownloadRequest
from finances_core.tasks import SQLTaskManager

from finances_downloader.ft_market_data_yfinance import service
from finances_downloader.ft_market_data_yfinance.planner import load_plan
from finances_downloader.ft_market_data_yfinance.service import (
    create_download_task,
    get_progress,
    get_result,
    run_download,
)
from finances_downloader.ft_market_data_yfinance.timeframes import DAY_MS

from .conftest import FakeYahoo, all_days, business_days, intraday, ms, yahoo_frame

TASK = "0192f000-0000-7000-8000-0000000000a1"


def _request(**overrides: Any) -> YFDownloadRequest:
    data: dict[str, Any] = {
        "symbols": ["AAPL"],
        "timeframes": ["1d"],
        "start_ms": ms(2024, 1, 1),
        "end_ms": ms(2024, 3, 1),
    }
    data.update(overrides)
    return YFDownloadRequest.model_validate(data)


def _aapl_daily(fake: FakeYahoo) -> None:
    fake.add(
        "AAPL",
        "1d",
        yahoo_frame(business_days("AAPL", "2024-01-02", "2024-02-29"), dividends={10: 0.24}),
    )


def _parquet_rows(base: Path) -> int:
    files = sorted(base.rglob("data.parquet"))
    return sum(pl.read_parquet(f).height for f in files)


async def test_completed_with_stats_per_symbol_and_timeframe(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    _aapl_daily(use_fake_yahoo)
    use_fake_yahoo.add(
        "BTC-USD", "1d", yahoo_frame(all_days("BTC-USD", "2024-01-01", "2024-02-29"))
    )
    phases: list[str] = []
    result = await run_download(
        _request(symbols=["AAPL", "BTC-USD"]), TASK, settings=settings, on_phase=phases.append
    )
    assert result.status == "COMPLETED"
    assert phases == ["PLANNING", "DOWNLOADING", "NORMALIZING", "WRITING_PARQUET", "COMPLETED"]
    assert result.stats["AAPL/1d"] == {"candles": 43, "market_type": "equity", "requests": 1}
    assert result.stats["BTC-USD/1d"] == {"candles": 60, "market_type": "crypto", "requests": 1}
    assert len(result.parquet_paths) == 4
    assert result.errors == []
    assert result.events_paths == [
        str(settings.PARQUET_DIR / "yfinance_events" / "symbol=AAPL" / "events.parquet")
    ]

    progress = get_progress(TASK, settings=settings)
    assert progress is not None
    assert progress.status == "COMPLETED"
    assert (progress.items_total, progress.items_done) == (2, 2)
    assert progress.processed_candles == 103
    assert progress.events_written == 1
    assert progress.gaps_found == 2

    assert get_result(TASK, settings=settings) == result
    assert load_plan(TASK, settings) is not None


async def test_rerun_is_lossless_and_only_requests_after_last_candle(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    _aapl_daily(use_fake_yahoo)
    first = await run_download(_request(end_ms=ms(2024, 2, 1)), TASK, settings=settings)
    assert first.status == "COMPLETED"
    base = settings.PARQUET_DIR / "yfinance"
    rows_before = _parquet_rows(base)

    use_fake_yahoo.calls.clear()
    second = await run_download(
        _request(start_ms=ms(2024, 1, 15)),
        "0192f000-0000-7000-8000-0000000000a2",
        settings=settings,
    )
    assert second.status == "COMPLETED"
    (call,) = use_fake_yahoo.calls
    last_jan = ms(2024, 1, 31, 5)
    assert last_jan // 1000 < call["start"] <= (last_jan + DAY_MS) // 1000
    assert _parquet_rows(base) == rows_before + 21  # febrero 2024: 21 días hábiles
    events = pl.read_parquet(
        settings.PARQUET_DIR / "yfinance_events" / "symbol=AAPL" / "events.parquet"
    )
    assert events.height == 1


async def test_market_closures_are_not_gaps_nor_errors(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    """RF-6: un fin de semana sin velas no es error; dentro de los datos no se re-pide."""
    _aapl_daily(use_fake_yahoo)
    # Ventana íntegramente en fin de semana: Yahoo "no price data found" -> procesada.
    weekend = await run_download(
        _request(start_ms=ms(2024, 1, 6), end_ms=ms(2024, 1, 7, 23)), TASK, settings=settings
    )
    assert weekend.status == "COMPLETED"
    assert weekend.errors == []
    assert weekend.gaps_unfillable == []
    assert weekend.stats["AAPL/1d"]["candles"] == 0

    await run_download(_request(), "0192f000-0000-7000-8000-0000000000a3", settings=settings)
    use_fake_yahoo.calls.clear()
    covered = await run_download(
        _request(start_ms=ms(2024, 1, 2, 5), end_ms=ms(2024, 1, 31, 12)),
        "0192f000-0000-7000-8000-0000000000a4",
        settings=settings,
    )
    assert covered.status == "COMPLETED"
    assert use_fake_yahoo.calls == []
    progress = get_progress("0192f000-0000-7000-8000-0000000000a4", settings=settings)
    assert progress is not None
    assert progress.gaps_found == 0


async def test_beyond_lookback_is_unfillable_without_request(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    now = ms(2026, 10, 25, 12)
    use_fake_yahoo.add(
        "BTC-USD", "5m", yahoo_frame(intraday("BTC-USD", "2026-08-01", "2026-10-25 11:00", "5min"))
    )
    result = await run_download(
        _request(symbols=["BTC-USD"], timeframes=["5m"], start_ms=now - 90 * DAY_MS, end_ms=now),
        TASK,
        settings=settings,
        now_ms=now,
    )
    assert result.status == "COMPLETED"
    (gap,) = result.gaps_unfillable
    assert gap.reason == "beyond_yahoo_lookback"
    assert gap.end_ms == now - 60 * DAY_MS + 3_600_000
    for call in use_fake_yahoo.calls:
        assert call["start"] * 1000 >= gap.end_ms


async def test_429_retries_then_succeeds(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    _aapl_daily(use_fake_yahoo)
    use_fake_yahoo.rate_limited["AAPL"] = 2
    result = await run_download(_request(), TASK, settings=settings)
    assert result.status == "COMPLETED"
    assert len(use_fake_yahoo.calls) == 3
    assert result.stats["AAPL/1d"]["requests"] == 1


async def test_persistent_429_fails_and_keeps_written_data(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    _aapl_daily(use_fake_yahoo)
    use_fake_yahoo.add(
        "BTC-USD", "1d", yahoo_frame(all_days("BTC-USD", "2024-01-01", "2024-02-29"))
    )
    await run_download(_request(end_ms=ms(2024, 2, 1)), TASK, settings=settings)
    jan_file = (
        settings.PARQUET_DIR
        / "yfinance/market_type=equity/symbol=AAPL/timeframe=1d/year=2024/month=01/data.parquet"
    )
    jan_before = pl.read_parquet(jan_file)

    use_fake_yahoo.rate_limited["AAPL"] = -1
    use_fake_yahoo.calls.clear()
    task = "0192f000-0000-7000-8000-0000000000b1"
    result = await run_download(_request(symbols=["AAPL", "BTC-USD"]), task, settings=settings)

    assert result.status == "FAILED"
    assert len(use_fake_yahoo.calls_for("AAPL")) == settings.YFINANCE_MAX_RETRIES + 1
    assert any("AAPL" in e and "429" in e for e in result.errors)
    assert pl.read_parquet(jan_file).equals(jan_before)
    # El otro símbolo se descargó y escribió igualmente.
    assert result.stats["BTC-USD/1d"]["candles"] == 60
    assert any("symbol=BTC-USD" in p for p in result.parquet_paths)
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(task)
    assert row is not None
    assert row["status"] == "FAILED"
    assert "429" in row["error"]


async def test_unknown_symbol_completes_with_errors(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    _aapl_daily(use_fake_yahoo)
    result = await run_download(
        _request(symbols=["AAPL", "NOPE123"], timeframes=["1d", "1w"]), TASK, settings=settings
    )
    assert result.status == "COMPLETED"
    assert len(result.errors) == 1
    assert "NOPE123" in result.errors[0]
    # Se omite el símbolo: no se reintenta ni se piden sus otros timeframes.
    assert len(use_fake_yahoo.calls_for("NOPE123")) == 1
    assert result.stats["AAPL/1d"]["candles"] == 43
    progress = get_progress(TASK, settings=settings)
    assert progress is not None
    assert progress.errors == result.errors


async def test_one_minute_windows_are_all_requested(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    now = ms(2026, 10, 25, 12)
    use_fake_yahoo.add(
        "BTC-USD", "1m", yahoo_frame(intraday("BTC-USD", "2026-10-05", "2026-10-25 11:00", "1min"))
    )
    result = await run_download(
        _request(symbols=["BTC-USD"], timeframes=["1m"], start_ms=now - 20 * DAY_MS, end_ms=now),
        TASK,
        settings=settings,
        now_ms=now,
    )
    assert result.status == "COMPLETED"
    assert len(use_fake_yahoo.calls) == 3
    assert result.stats["BTC-USD/1m"]["requests"] == 3
    assert result.stats["BTC-USD/1m"]["candles"] == 20 * 24 * 60 - 60


async def test_concurrency_is_bounded(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading  # noqa: PLC0415
    import time  # noqa: PLC0415

    active = 0
    peak = 0
    lock = threading.Lock()
    inner = use_fake_yahoo.fetcher

    def slow_fetcher(*args: Any) -> Any:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        try:
            return inner(*args)
        finally:
            with lock:
                active -= 1

    for symbol in ("AAPL", "SPY", "^GSPC", "GC=F"):
        use_fake_yahoo.add(
            symbol, "1d", yahoo_frame(business_days(symbol, "2024-01-02", "2024-01-31"))
        )
    result = await run_download(
        _request(symbols=["AAPL", "SPY", "^GSPC", "GC=F"], max_concurrent=2),
        TASK,
        settings=settings,
        fetcher=slow_fetcher,
    )
    assert result.status == "COMPLETED"
    assert peak <= 2
    assert {result.stats[f"{s}/1d"]["market_type"] for s in ("SPY", "^GSPC", "GC=F")} == {
        "etf",
        "index",
        "future",
    }


async def test_rate_limiter_is_built_from_settings(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    bucket = service.build_rate_limiter(settings)
    assert (bucket.capacity, bucket.refill_rate) == (1000, 1000.0)


async def test_planning_failure_marks_failed(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise OSError("disk gone")

    monkeypatch.setattr(service, "build_download_plan", boom)
    result = await run_download(_request(), TASK, settings=settings)
    assert result.status == "FAILED"
    assert "disk gone" in result.errors[0]


async def test_unexpected_error_marks_failed_and_propagates(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    def broken(*_: Any) -> Any:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await run_download(_request(), TASK, settings=settings, fetcher=broken)
    progress = get_progress(TASK, settings=settings)
    assert progress is not None
    assert progress.status == "FAILED"


def test_pending_task_progress_and_result_not_finished(settings: FinancesSettings) -> None:
    create_download_task(TASK, _request(symbols=["AAPL", "SPY"]), settings=settings)
    progress = get_progress(TASK, settings=settings)
    assert progress is not None
    assert progress.status == "PENDING"
    assert progress.items_total == 0
    with pytest.raises(service.TaskNotFinishedError):
        get_result(TASK, settings=settings)
    assert get_progress("0192f000-0000-7000-8000-0000000000ff", settings=settings) is None
    assert get_result("0192f000-0000-7000-8000-0000000000ff", settings=settings) is None


def test_run_download_sync_never_raises(
    use_fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    _aapl_daily(use_fake_yahoo)
    service.run_download_sync(_request(), TASK, settings)
    result = get_result(TASK, settings=settings)
    assert result is not None
    assert result.status == "COMPLETED"
