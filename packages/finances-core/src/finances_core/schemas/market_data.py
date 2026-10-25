from enum import StrEnum
from typing import Literal

import polars as pl
from pydantic import BaseModel, Field, ValidationInfo, field_validator


class MarketType(StrEnum):
    SPOT = "spot"
    FUTURES = "futures"


class Timeframe(StrEnum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    M1_MONTH = "1M"


class DownloadRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=100)
    timeframes: list[Timeframe] = Field(min_length=1)
    start_ms: int = Field(gt=0)
    end_ms: int | None = Field(default=None, gt=0)
    market_type: MarketType = MarketType.SPOT
    limit_per_request: int = Field(default=1000, ge=1, le=1000)
    max_concurrent: int | None = Field(default=None, ge=1, le=50)

    @field_validator("end_ms")
    @classmethod
    def validate_time_range(cls, v: int | None, info: ValidationInfo) -> int | None:
        if v is not None and info.data.get("start_ms") and v <= info.data["start_ms"]:
            raise ValueError("end_ms must be greater than start_ms")  # noqa: TRY003
        return v


class DownloadPlanItem(BaseModel):
    source: Literal["public_data", "rest_api"]
    symbol: str
    timeframe: str
    market_type: str
    url: str | None = None
    expected_checksum: str | None = None
    endpoint: str | None = None
    params: dict[str, object] | None = None
    covers_range_ms: list[int] = Field(min_length=2, max_length=2)


class DownloadPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: list[DownloadPlanItem]


class DownloadProgress(BaseModel):
    task_id: str
    status: Literal[
        "PENDING",
        "PLANNING",
        "DOWNLOADING_PUBLIC",
        "DOWNLOADING_REST",
        "NORMALIZING",
        "WRITING_PARQUET",
        "COMPLETED",
        "FAILED",
    ]
    phase: str
    symbols_total: int
    symbols_done: int
    timeframes_total: int
    timeframes_done: int
    downloaded_bytes: int = 0
    processed_candles: int = 0
    gaps_found: int = 0
    gaps_filled_public: int = 0
    gaps_filled_rest: int = 0
    errors: list[str] = []
    current_symbol: str | None = None
    current_timeframe: str | None = None


class DownloadResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    parquet_paths: list[str]
    stats: dict[str, object]
    errors: list[str] = Field(default_factory=list)
    gaps_unfillable: list[dict[str, object]] = []


OHLCV_SCHEMA = {
    "symbol": pl.Utf8,
    "timeframe": pl.Utf8,
    "market_type": pl.Utf8,
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "volume": pl.Float32,
    "quote_volume": pl.Float32,
    "trades_count": pl.Int64,
    "taker_buy_base_volume": pl.Float32,
    "taker_buy_quote_volume": pl.Float32,
}

OHLCV_PARTITION_COLS = ["symbol", "timeframe", "market_type", "year", "month"]


TF_MS_MAP = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}
