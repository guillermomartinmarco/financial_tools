from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Dict, Any


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"


class DatasetIdentifier(BaseModel):
    exchange: str = "binance"
    market_type: Literal["spot", "futures"]
    symbol: str
    timeframe: str


class DatasetInfo(BaseModel):
    id: DatasetIdentifier
    year_range: tuple[int, int]
    month_count: int
    estimated_rows: int
    size_bytes: int
    last_modified_ms: int
    partition_path: str


class DatasetListResponse(BaseModel):
    datasets: List[DatasetInfo]
    total_count: int
    total_size_bytes: int
    total_rows: int


class ColumnStats(BaseModel):
    name: str
    dtype: str
    null_count: int
    distinct_count: Optional[int] = None
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    std: Optional[float] = None


class DatasetSummary(BaseModel):
    dataset_id: DatasetIdentifier
    row_count: int
    date_range: tuple[int, int]
    columns: List[ColumnStats]
    gaps_detected: int
    duplicate_timestamps: int
    outlier_count: int


class GapInfo(BaseModel):
    gap_start_ms: int
    gap_end_ms: int
    missing_candles: int
    gap_duration_human: str


class AnomalyReport(BaseModel):
    dataset_id: DatasetIdentifier
    duplicate_timestamps: Dict[str, Any]
    null_values: Dict[str, int]
    price_outliers: Dict[str, List[Dict]]
    volume_anomalies: Dict[str, List[Dict]]


class ExportRequest(BaseModel):
    dataset_id: DatasetIdentifier
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    columns: Optional[List[str]] = None
    format: Literal["parquet", "csv", "jsonl"] = "parquet"
    output_dir: Optional[str] = None


class CompareRequest(BaseModel):
    dataset_a: DatasetIdentifier
    dataset_b: DatasetIdentifier
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


class CompareResult(BaseModel):
    dataset_a: DatasetIdentifier
    dataset_b: DatasetIdentifier
    row_count_diff: int
    date_range_overlap: Optional[tuple[int, int]]
    price_divergence_max: float
    volume_divergence_pct: float
    gaps_only_in_a: List[GapInfo]
    gaps_only_in_b: List[GapInfo]