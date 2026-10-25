from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict

#: Formato textual de un dataset (CLI de ``ft_indicators``/``ft_ta_lib``).
DATASET_PATH_FORMAT = "exchange/market_type/symbol/timeframe"
_DATASET_SEGMENTS = 4


class DatasetIdentifier(BaseModel):
    # Inmutable y *hashable*: clave de ``items_by_dataset`` en ``indicators_store.runner``.
    model_config = ConfigDict(frozen=True)

    exchange: str = "binance"
    # Binance usa spot/futures; Yahoo Finance (ft_market_data_yfinance) el resto.
    market_type: Literal[
        "spot", "futures", "equity", "etf", "index", "fx", "crypto", "future", "fund", "other"
    ]
    symbol: str
    timeframe: str

    @classmethod
    def parse(cls, text: str) -> Self:
        """Construye el identificador desde ``"exchange/market_type/symbol/timeframe"``.

        Raises:
            ValueError: SI faltan segmentos, alguno está vacío o ``market_type`` no es
                válido (``pydantic.ValidationError`` es un ``ValueError``).
        """
        parts = text.strip().split("/")
        if len(parts) != _DATASET_SEGMENTS or not all(parts):
            msg = f"Dataset inválido {text!r}: se espera {DATASET_PATH_FORMAT}"
            raise ValueError(msg)
        exchange, market_type, symbol, timeframe = parts
        return cls.model_validate(
            {
                "exchange": exchange,
                "market_type": market_type,
                "symbol": symbol,
                "timeframe": timeframe,
            }
        )

    def path_segment(self) -> str:
        """Inverso de :meth:`parse`: ``"exchange/market_type/symbol/timeframe"``."""
        return f"{self.exchange}/{self.market_type}/{self.symbol}/{self.timeframe}"


class DatasetInfo(BaseModel):
    id: DatasetIdentifier
    year_range: tuple[int, int]
    month_count: int
    estimated_rows: int
    size_bytes: int
    last_modified_ms: int
    partition_path: str


class DatasetListResponse(BaseModel):
    datasets: list[DatasetInfo]
    total_count: int
    total_size_bytes: int
    total_rows: int


class ColumnStats(BaseModel):
    name: str
    dtype: str
    null_count: int
    distinct_count: int | None = None
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None


class DatasetSummary(BaseModel):
    dataset_id: DatasetIdentifier
    row_count: int
    date_range: tuple[int, int]
    columns: list[ColumnStats]
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
    duplicate_timestamps: dict[str, Any]
    null_values: dict[str, int]
    price_outliers: dict[str, list[dict[str, Any]]]
    volume_anomalies: dict[str, list[dict[str, Any]]]


class ExportRequest(BaseModel):
    # Opcional en la API: el dataset ya viene en la ruta /datasets/{...}/export.
    dataset_id: DatasetIdentifier | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    columns: list[str] | None = None
    format: Literal["parquet", "csv", "jsonl"] = "parquet"
    output_dir: str | None = None


class CompareRequest(BaseModel):
    dataset_a: DatasetIdentifier
    dataset_b: DatasetIdentifier
    start_ms: int | None = None
    end_ms: int | None = None


class CompareResult(BaseModel):
    dataset_a: DatasetIdentifier
    dataset_b: DatasetIdentifier
    row_count_diff: int
    date_range_overlap: tuple[int, int] | None
    price_divergence_max: float
    volume_divergence_pct: float
    gaps_only_in_a: list[GapInfo]
    gaps_only_in_b: list[GapInfo]
