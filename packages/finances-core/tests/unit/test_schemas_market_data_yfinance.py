"""T1.5: contratos ``market_data_yfinance`` y settings ``FT_YFINANCE_*``."""

from __future__ import annotations

import polars as pl
import pytest
from pydantic import ValidationError

from finances_core.config import FinancesSettings
from finances_core.schemas.dispatch import DispatchResponse
from finances_core.schemas.market_data_yfinance import (
    INSTRUMENT_TYPE_MAP,
    YF_EVENTS_SCHEMA,
    YF_OHLCV_SCHEMA,
    YFDownloadPlan,
    YFDownloadProgress,
    YFDownloadRequest,
    YFDownloadResult,
    YFPlanItem,
    YFUnfillableGap,
)


def test_request_defaults() -> None:
    req = YFDownloadRequest(symbols=["AAPL", "^GSPC", "EURUSD=X"], timeframes=["1d"], start_ms=1)
    assert req.end_ms is None
    assert req.max_concurrent is None
    assert req.symbols == ["AAPL", "^GSPC", "EURUSD=X"]


@pytest.mark.parametrize("tf", ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"])
def test_request_accepts_canonical_timeframes(tf: str) -> None:
    assert YFDownloadRequest(symbols=["AAPL"], timeframes=[tf], start_ms=1).timeframes == [tf]


@pytest.mark.parametrize("tf", ["2m", "90m", "4h", "5d", "1wk", "1mo", "3mo"])
def test_request_rejects_non_canonical_timeframe(tf: str) -> None:
    with pytest.raises(ValidationError):
        YFDownloadRequest(symbols=["AAPL"], timeframes=[tf], start_ms=1)


def test_request_symbols_between_1_and_100() -> None:
    with pytest.raises(ValidationError):
        YFDownloadRequest(symbols=[], timeframes=["1d"], start_ms=1)
    with pytest.raises(ValidationError):
        YFDownloadRequest(symbols=[f"S{i}" for i in range(101)], timeframes=["1d"], start_ms=1)
    ok = YFDownloadRequest(symbols=[f"S{i}" for i in range(100)], timeframes=["1d"], start_ms=1)
    assert len(ok.symbols) == 100


def test_request_rejects_empty_timeframes_and_bad_range() -> None:
    with pytest.raises(ValidationError):
        YFDownloadRequest(symbols=["AAPL"], timeframes=[], start_ms=1)
    with pytest.raises(ValidationError):
        YFDownloadRequest(symbols=["AAPL"], timeframes=["1d"], start_ms=10, end_ms=5)
    with pytest.raises(ValidationError):
        YFDownloadRequest(symbols=["AAPL"], timeframes=["1d"], start_ms=1, max_concurrent=0)


def test_plan_progress_result_roundtrip() -> None:
    item = YFPlanItem(symbol="AAPL", timeframe="1w", yahoo_interval="1wk", start_ms=1, end_ms=2)
    gap = YFUnfillableGap(
        symbol="AAPL", timeframe="1m", start_ms=1, end_ms=2, reason="beyond_yahoo_lookback"
    )
    plan = YFDownloadPlan(task_id="t", created_at_ms=0, items=[item], gaps_unfillable=[gap])
    assert YFDownloadPlan.model_validate_json(plan.model_dump_json()) == plan
    progress = YFDownloadProgress(
        task_id="t",
        status="DOWNLOADING",
        items_total=1,
        items_done=0,
        processed_candles=0,
        events_written=0,
        gaps_found=1,
        errors=[],
    )
    assert progress.status == "DOWNLOADING"
    with pytest.raises(ValidationError):
        YFDownloadProgress.model_validate({**progress.model_dump(), "status": "DOWNLOADING_REST"})
    result = YFDownloadResult(
        task_id="t",
        status="COMPLETED",
        parquet_paths=[],
        events_paths=[],
        stats={},
        errors=[],
        gaps_unfillable=[gap],
    )
    assert result.gaps_unfillable[0].reason == "beyond_yahoo_lookback"
    with pytest.raises(ValidationError):
        YFUnfillableGap(symbol="A", timeframe="1d", start_ms=1, end_ms=2, reason="other")


def test_polars_schemas_and_instrument_map() -> None:
    assert list(YF_OHLCV_SCHEMA) == [
        "symbol",
        "timeframe",
        "market_type",
        "open_time_ms",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    ]
    assert YF_OHLCV_SCHEMA["open_time_ms"] == pl.Int64
    assert YF_OHLCV_SCHEMA["adj_close"] == pl.Float32
    assert list(YF_EVENTS_SCHEMA.items()) == [
        ("event_time_ms", pl.Int64),
        ("event_type", pl.Utf8),
        ("value", pl.Float64),
    ]
    assert INSTRUMENT_TYPE_MAP["CRYPTOCURRENCY"] == "crypto"
    assert INSTRUMENT_TYPE_MAP["CURRENCY"] == "fx"
    assert set(INSTRUMENT_TYPE_MAP.values()) == {
        "equity",
        "etf",
        "index",
        "fx",
        "crypto",
        "future",
        "fund",
    }


def test_dispatch_response_defaults_to_accepted() -> None:
    resp = DispatchResponse(task_id="t", poll_url="/s", plan_url="/p", result_url="/r")
    assert resp.model_dump() == {
        "task_id": "t",
        "status": "ACCEPTED",
        "poll_url": "/s",
        "plan_url": "/p",
        "result_url": "/r",
    }


def test_yfinance_settings_defaults() -> None:
    s = FinancesSettings()
    assert s.YFINANCE_RATE_CAPACITY == 5
    assert s.YFINANCE_RATE_REFILL_RATE == 1.0
    assert s.YFINANCE_MAX_RETRIES == 3
    assert s.YFINANCE_BASE_BACKOFF_MS == 2000
    assert s.YFINANCE_TIMEOUT_S == 30
    assert not hasattr(s, "YFINANCE_API_KEY")


def test_yfinance_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_YFINANCE_MAX_RETRIES", "7")
    monkeypatch.setenv("FT_YFINANCE_RATE_REFILL_RATE", "0.5")
    s = FinancesSettings()
    assert s.YFINANCE_MAX_RETRIES == 7
    assert s.YFINANCE_RATE_REFILL_RATE == 0.5
