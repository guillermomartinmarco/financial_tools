"""Contratos de ``ft_market_data_yfinance`` (plan.md §1.1/§1.2).

Timeframes canónicos del proyecto (RF-2); en disco y en la API se usan siempre
estos nombres, la traducción al intervalo de Yahoo vive en la herramienta.
"""

from __future__ import annotations

from typing import Literal

import polars as pl
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from finances_core.schemas.dispatch import DispatchResponse

YFTimeframe = Literal["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"]
YFPhase = Literal[
    "PENDING",
    "PLANNING",
    "DOWNLOADING",
    "NORMALIZING",
    "WRITING_PARQUET",
    "COMPLETED",
    "FAILED",
]

#: Orden de fases RF-8 (sin estados terminales), para ``TaskRecorder``.
YF_PHASE_ORDER: tuple[str, ...] = (
    "PENDING",
    "PLANNING",
    "DOWNLOADING",
    "NORMALIZING",
    "WRITING_PARQUET",
)


class YFDownloadRequest(BaseModel):
    """Petición de descarga (símbolos tal cual Yahoo: ``AAPL``, ``^GSPC``, ``EURUSD=X``)."""

    symbols: list[str] = Field(min_length=1, max_length=100)
    timeframes: list[YFTimeframe] = Field(min_length=1)
    start_ms: int = Field(gt=0)
    end_ms: int | None = Field(default=None, gt=0)  # None = now
    max_concurrent: int | None = Field(default=None, ge=1, le=50)

    @field_validator("end_ms")
    @classmethod
    def validate_time_range(cls, v: int | None, info: ValidationInfo) -> int | None:
        if v is not None and info.data.get("start_ms") and v <= info.data["start_ms"]:
            msg = "end_ms must be greater than start_ms"
            raise ValueError(msg)
        return v


class YFPlanItem(BaseModel):
    symbol: str
    timeframe: YFTimeframe
    yahoo_interval: str  # 1m, 5m, ..., 1wk, 1mo
    start_ms: int
    end_ms: int  # exclusivo


class YFUnfillableGap(BaseModel):
    symbol: str
    timeframe: YFTimeframe
    start_ms: int
    end_ms: int
    reason: Literal["beyond_yahoo_lookback"]


class YFDownloadPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: list[YFPlanItem]
    gaps_unfillable: list[YFUnfillableGap] = Field(default_factory=list)


class YFDownloadProgress(BaseModel):
    task_id: str
    status: YFPhase
    items_total: int
    items_done: int
    processed_candles: int = 0
    events_written: int = 0
    gaps_found: int = 0
    errors: list[str] = Field(default_factory=list)


class YFDownloadResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    parquet_paths: list[str]
    events_paths: list[str] = Field(default_factory=list)
    stats: dict[str, object]  # por símbolo/timeframe: candles, market_type
    errors: list[str] = Field(default_factory=list)
    gaps_unfillable: list[YFUnfillableGap] = Field(default_factory=list)


YF_OHLCV_SCHEMA = {
    "symbol": pl.Utf8,
    "timeframe": pl.Utf8,
    "market_type": pl.Utf8,
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "adj_close": pl.Float32,
    "volume": pl.Float32,
}

YF_EVENTS_SCHEMA = {
    "event_time_ms": pl.Int64,
    "event_type": pl.Utf8,
    "value": pl.Float64,
}

#: ``instrumentType`` de Yahoo → ``market_type`` en disco (desconocido → ``other``).
INSTRUMENT_TYPE_MAP = {
    "EQUITY": "equity",
    "ETF": "etf",
    "INDEX": "index",
    "CURRENCY": "fx",
    "CRYPTOCURRENCY": "crypto",
    "FUTURE": "future",
    "MUTUALFUND": "fund",
}

__all__ = [
    "INSTRUMENT_TYPE_MAP",
    "YF_EVENTS_SCHEMA",
    "YF_OHLCV_SCHEMA",
    "YF_PHASE_ORDER",
    "DispatchResponse",
    "YFDownloadPlan",
    "YFDownloadProgress",
    "YFDownloadRequest",
    "YFDownloadResult",
    "YFPhase",
    "YFPlanItem",
    "YFTimeframe",
    "YFUnfillableGap",
]
