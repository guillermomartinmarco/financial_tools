from pathlib import Path
from typing import Optional, List, Literal, Union, IO, Dict
import structlog
import polars as pl

from finances_core.schemas.data_viewer import (
    DatasetIdentifier, DatasetInfo, DatasetListResponse, DatasetSummary,
    GapInfo, AnomalyReport, ExportRequest, CompareRequest, CompareResult,
    ColumnStats
)
from finances_core.schemas.market_data import TF_MS_MAP
from finances_core.config import settings

from finances_downloader.ft_data_viewer.scanner import scan_parquet_partitions, resolve_dataset_path, read_dataset_metadata
from finances_downloader.ft_data_viewer.analyzer import compute_summary, detect_gaps, detect_anomalies, sample_rows
from finances_downloader.ft_data_viewer.exporter import stream_export, export_to_stdout
from finances_downloader.ft_data_viewer.comparator import compare_datasets
from finances_downloader.ft_data_viewer.models import DatasetListResponse as APIListResponse

logger = structlog.get_logger(__name__)


class DatasetNotFoundError(Exception):
    """Excepción cuando dataset no existe."""
    pass


def list_datasets(
    exchange: Optional[str] = None,
    market_type: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> DatasetListResponse:
    """Lista datasets disponibles con filtros opcionales."""
    datasets = scan_parquet_partitions(settings.PARQUET_DIR)

    # Aplicar filtros
    if exchange:
        datasets = [d for d in datasets if d.id.exchange.lower() == exchange.lower()]
    if market_type:
        datasets = [d for d in datasets if d.id.market_type == market_type]
    if symbol:
        datasets = [d for d in datasets if d.id.symbol.upper() == symbol.upper()]
    if timeframe:
        datasets = [d for d in datasets if d.id.timeframe == timeframe]

    total_size = sum(d.size_bytes for d in datasets)
    total_rows = sum(d.estimated_rows for d in datasets)

    return DatasetListResponse(
        datasets=datasets,
        total_count=len(datasets),
        total_size_bytes=total_size,
        total_rows=total_rows,
    )


def get_summary(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
) -> DatasetSummary:
    """Obtiene resumen estadístico de un dataset."""
    identifier = DatasetIdentifier(
        exchange=exchange.lower(),
        market_type=market_type,
        symbol=symbol.upper(),
        timeframe=timeframe,
    )
    dataset_path = resolve_dataset_path(settings.PARQUET_DIR, identifier)

    if not dataset_path.exists():
        raise DatasetNotFoundError(f"Dataset no encontrado: {identifier}")

    lf = pl.scan_parquet(dataset_path, hive_partitioning=True)
    # Filtrar por el identificador específico
    lf = lf.filter(
        (pl.col("symbol") == identifier.symbol) &
        (pl.col("market_type") == identifier.market_type) &
        (pl.col("timeframe") == identifier.timeframe)
    )
    summary = compute_summary(lf, identifier)
    return summary


def get_rows(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    mode: Literal["head", "tail", "sample"] = "head",
    n: int = 10,
    cols: Optional[List[str]] = None,
) -> List[Dict]:
    """Obtiene filas muestra (head/tail/sample)."""
    identifier = DatasetIdentifier(
        exchange=exchange.lower(),
        market_type=market_type,
        symbol=symbol.upper(),
        timeframe=timeframe,
    )
    dataset_path = resolve_dataset_path(settings.PARQUET_DIR, identifier)

    if not dataset_path.exists():
        raise DatasetNotFoundError(f"Dataset no encontrado: {identifier}")

    lf = pl.scan_parquet(dataset_path, hive_partitioning=True)
    lf = lf.filter(
        (pl.col("symbol") == identifier.symbol) &
        (pl.col("market_type") == identifier.market_type) &
        (pl.col("timeframe") == identifier.timeframe)
    )
    df = sample_rows(lf, n=n, mode=mode, cols=cols)
    return df.to_dicts()


def get_gaps(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    min_gap_candles: int = 1,
) -> List[Dict]:
    """Detecta gaps temporales."""
    identifier = DatasetIdentifier(
        exchange=exchange.lower(),
        market_type=market_type,
        symbol=symbol.upper(),
        timeframe=timeframe,
    )
    dataset_path = resolve_dataset_path(settings.PARQUET_DIR, identifier)

    if not dataset_path.exists():
        raise DatasetNotFoundError(f"Dataset no encontrado: {identifier}")

    lf = pl.scan_parquet(dataset_path, hive_partitioning=True)
    lf = lf.filter(
        (pl.col("symbol") == identifier.symbol) &
        (pl.col("market_type") == identifier.market_type) &
        (pl.col("timeframe") == identifier.timeframe)
    )
    timeframe_ms = TF_MS_MAP.get(timeframe, 60_000)
    gaps = detect_gaps(lf, timeframe_ms, min_gap_candles)
    return [g.model_dump() for g in gaps]


def get_anomalies(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
) -> AnomalyReport:
    """Detecta anomalías en el dataset."""
    logger.debug("get_anomalies called", exchange=exchange, market_type=market_type, symbol=symbol, timeframe=timeframe)
    identifier = DatasetIdentifier(
        exchange=exchange.lower(),
        market_type=market_type,
        symbol=symbol.upper(),
        timeframe=timeframe,
    )
    logger.debug("identifier created", identifier=identifier.model_dump())
    dataset_path = resolve_dataset_path(settings.PARQUET_DIR, identifier)

    if not dataset_path.exists():
        raise DatasetNotFoundError(f"Dataset no encontrado: {identifier}")

    lf = pl.scan_parquet(dataset_path, hive_partitioning=True)
    lf = lf.filter(
        (pl.col("symbol") == identifier.symbol) &
        (pl.col("market_type") == identifier.market_type) &
        (pl.col("timeframe") == identifier.timeframe)
    )
    report = detect_anomalies(lf, identifier)
    report.dataset_id = identifier
    return report


def export_data(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    cols: Optional[List[str]] = None,
    format: Literal["parquet", "csv", "jsonl"] = "parquet",
    output_dir: Optional[str] = None,
) -> Union[str, int]:
    """
    Exporta subconjunto filtrado.
    Retorna path de salida o row count si stdout.
    """
    identifier = DatasetIdentifier(
        exchange=exchange.lower(),
        market_type=market_type,
        symbol=symbol.upper(),
        timeframe=timeframe,
    )
    dataset_path = resolve_dataset_path(settings.PARQUET_DIR, identifier)

    if not dataset_path.exists():
        raise DatasetNotFoundError(f"Dataset no encontrado: {identifier}")

    lf = pl.scan_parquet(dataset_path, hive_partitioning=True)
    lf = lf.filter(
        (pl.col("symbol") == identifier.symbol) &
        (pl.col("market_type") == identifier.market_type) &
        (pl.col("timeframe") == identifier.timeframe)
    )

    # Filtrar por rango temporal
    if start_ms is not None:
        lf = lf.filter(pl.col("open_time_ms") >= start_ms)
    if end_ms is not None:
        lf = lf.filter(pl.col("open_time_ms") <= end_ms)

    # Verificar que hay datos
    count = lf.select(pl.len()).collect()["len"][0]
    if count == 0:
        raise ValueError("No hay datos en el rango especificado")

    if output_dir:
        output_path = Path(output_dir) / f"{exchange}_{market_type}_{symbol}_{timeframe}.{format}"
        rows = stream_export(lf, format, output_path, cols)
        return str(output_path)
    else:
        # Exportar a stdout
        rows = export_to_stdout(lf, format, cols)
        return rows


def compare_datasets_service(
    dataset_a: DatasetIdentifier,
    dataset_b: DatasetIdentifier,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> CompareResult:
    """Compara dos datasets."""
    path_a = resolve_dataset_path(settings.PARQUET_DIR, dataset_a)
    path_b = resolve_dataset_path(settings.PARQUET_DIR, dataset_b)

    if not path_a.exists():
        raise DatasetNotFoundError(f"Dataset A no encontrado: {dataset_a}")
    if not path_b.exists():
        raise DatasetNotFoundError(f"Dataset B no encontrado: {dataset_b}")

    lf_a = pl.scan_parquet(path_a, hive_partitioning=True)
    lf_b = pl.scan_parquet(path_b, hive_partitioning=True)

    lf_a = lf_a.filter(
        (pl.col("symbol") == dataset_a.symbol) &
        (pl.col("market_type") == dataset_a.market_type) &
        (pl.col("timeframe") == dataset_a.timeframe)
    )
    lf_b = lf_b.filter(
        (pl.col("symbol") == dataset_b.symbol) &
        (pl.col("market_type") == dataset_b.market_type) &
        (pl.col("timeframe") == dataset_b.timeframe)
    )

    return compare_datasets(lf_a, lf_b, dataset_a, dataset_b, start_ms, end_ms)