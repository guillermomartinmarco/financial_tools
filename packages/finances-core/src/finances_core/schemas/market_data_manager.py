"""Contratos Pydantic de ``ft_market_data_manager`` (plan 1.1).

Compartidos por la CLI, la API y el servicio. El selector usa listas combinadas con AND
entre campos; una lista vacía no filtra.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class DatasetSelector(BaseModel):
    """Misma identificación que ``ft_data_viewer``; las listas se combinan con AND."""

    exchange: list[str] = []
    market_type: list[str] = []
    symbol: list[str] = []
    timeframe: list[str] = []
    start_ms: int | None = None  # acota meses (inclusive)
    end_ms: int | None = None

    def has_dataset_filter(self) -> bool:
        """``True`` si algún campo de identificación de dataset está informado."""
        return bool(self.exchange or self.market_type or self.symbol or self.timeframe)


class PartitionResult(BaseModel):
    dataset: str  # "binance/spot/BTCUSDT/1h"
    year: int
    month: int
    result: Literal["changed", "unchanged", "conflict", "error", "planned"]
    files_before: int
    files_after: int
    rows_before: int
    rows_after: int
    bytes_before: int
    bytes_after: int
    detail: str | None = None  # error o ejemplo de duplicado


class ManagerRequest(DatasetSelector):
    dry_run: bool = False


class PruneRequest(ManagerRequest):
    before_ms: int | None = None
    all: bool = False
    confirm: bool = False  # API; la CLI usa --force / prompt

    @model_validator(mode="after")
    def _check_criterion(self) -> Self:
        if (self.before_ms is None) == (not self.all):
            msg = "prune exige exactamente uno de before_ms o all"
            raise ValueError(msg)
        if self.all and not self.has_dataset_filter():
            msg = (
                "prune --all exige algún filtro de dataset (exchange/market_type/symbol/timeframe)"
            )
            raise ValueError(msg)
        return self


class ImportRequest(BaseModel):
    source: Path  # fuera de FT_PARQUET_DIR
    format: Literal["parquet", "csv", "jsonl"] | None = None  # None => por extensión
    exchange: str = Field(min_length=1)  # destino explícito, nunca deducido del archivo
    market_type: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    create: bool = False  # permite crear el dataset destino
    ts_column: str = "open_time_ms"
    ts_unit: Literal["s", "ms", "us", "ns"] = "ms"
    on_conflict: Literal["skip", "fail", "replace"] = "skip"
    skip_invalid: bool = False
    dry_run: bool = False
    confirm: bool = False  # API; la CLI usa --force / prompt
    force: bool = False  # permite replace con divergencia > 1 %


class ImportPlan(BaseModel):
    destination: str  # "binance/spot/BTCUSDT/1h"
    rows_in_file: int
    rows_new: int
    rows_conflict: int
    rows_rejected: int
    range_ms: tuple[int, int]
    partitions_new: list[str]
    partitions_modified: list[str]
    max_close_divergence: float | None  # solo con conflictos


class ManagerTaskResponse(BaseModel):  # 202
    task_id: str
    poll_url: str


class ManagerResult(BaseModel):
    action: Literal["compact", "dedupe", "prune", "import"]
    dry_run: bool
    partitions: list[PartitionResult]
    import_plan: ImportPlan | None = None  # solo import
