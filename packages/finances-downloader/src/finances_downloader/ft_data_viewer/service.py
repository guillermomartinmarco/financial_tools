"""Orquestador compartido CLI/API (Art. 1, DV-07).

Cada operación: valida argumentos → resuelve dataset (scanner) → analiza/exporta/compara →
registra métricas RF-8 (``dataset_id``, ``duration_ms``, ``rows_scanned``, ``bytes_read``).
La configuración se lee de ``FinancesSettings()`` en cada llamada, nunca de un singleton
creado al importar (las variables ``FT_*`` vigentes siempre aplican).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import polars as pl
import structlog
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import (
    AnomalyReport,
    CompareResult,
    DatasetIdentifier,
    DatasetListResponse,
    DatasetSummary,
    ExportRequest,
    GapInfo,
)
from finances_core.schemas.market_data import TF_MS_MAP

from finances_downloader.ft_data_viewer.analyzer import (
    TS_COL,
    OutlierMethod,
    SampleMode,
    compute_summary,
    detect_anomalies,
    detect_gaps,
    sample_rows,
)
from finances_downloader.ft_data_viewer.comparator import compare_datasets
from finances_downloader.ft_data_viewer.errors import (
    InvalidArgumentError,
    NoDataInRangeError,
)
from finances_downloader.ft_data_viewer.exporter import ExportFormat, stream_export
from finances_downloader.ft_data_viewer.scanner import (
    MARKET_TYPES,
    ProgressCallback,
    dataset_label,
    dataset_size_bytes,
    read_dataset_metadata,
    read_row_count,
    resolve_dataset_path,
    scan_dataset,
    scan_parquet_partitions,
)

logger = structlog.get_logger(__name__, component="ft_data_viewer")


@dataclass(frozen=True)
class ExportOutcome:
    """Resultado de ``export_data``: filas exportadas y ruta (``None`` si fue a un stream)."""

    rows: int
    path: Path | None


# --- Validación compartida ------------------------------------------------------------


def make_identifier(
    exchange: str, market_type: str, symbol: str, timeframe: str
) -> DatasetIdentifier:
    """Normaliza (exchange en minúsculas, símbolo en mayúsculas) y valida un identificador.

    Raises:
        InvalidArgumentError: SI ``market_type`` o ``timeframe`` no son válidos.
    """
    if market_type not in MARKET_TYPES:
        msg = f"market_type inválido: {market_type!r} ({'|'.join(MARKET_TYPES)})"
        raise InvalidArgumentError(msg)
    if timeframe not in TF_MS_MAP:
        msg = f"timeframe inválido: {timeframe!r} ({'|'.join(TF_MS_MAP)})"
        raise InvalidArgumentError(msg)
    return DatasetIdentifier.model_validate(
        {
            "exchange": exchange.strip().lower(),
            "market_type": market_type,
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
        }
    )


def normalize_identifier(identifier: DatasetIdentifier) -> DatasetIdentifier:
    """Aplica ``make_identifier`` a un identificador ya construido (p. ej. cuerpo JSON)."""
    return make_identifier(
        identifier.exchange, identifier.market_type, identifier.symbol, identifier.timeframe
    )


def parse_columns(cols: str | None) -> list[str] | None:
    """``"open, close"`` → ``["open", "close"]`` (T5.5). ``None`` → sin proyección."""
    if cols is None:
        return None
    parsed = [c.strip() for c in cols.split(",") if c.strip()]
    if not parsed:
        msg = "--cols no contiene ninguna columna"
        raise InvalidArgumentError(msg)
    return parsed


def _check_columns(lf: pl.LazyFrame, cols: list[str] | None) -> None:
    if not cols:
        return
    available = lf.collect_schema().names()
    unknown = [c for c in cols if c not in available]
    if unknown:
        msg = f"Columnas desconocidas: {', '.join(unknown)} (disponibles: {', '.join(available)})"
        raise InvalidArgumentError(msg)


def _settings() -> FinancesSettings:
    return FinancesSettings()


@contextmanager
def _measure(operation: str, identifier: DatasetIdentifier, path: Path) -> Iterator[dict[str, Any]]:
    """Registra la métrica RF-8 al terminar la operación (éxito o fallo)."""
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    status = "error"
    try:
        yield extra
        status = "ok"
    finally:
        log = logger.info if status == "ok" else logger.warning
        log(
            "dataset_operation_complete",
            operation=operation,
            status=status,
            dataset_id=dataset_label(identifier),
            symbol=identifier.symbol,
            timeframe=identifier.timeframe,
            duration_ms=int((time.perf_counter() - started) * 1000),
            rows_scanned=read_row_count(path),
            bytes_read=dataset_size_bytes(path),
            **extra,
        )


# --- Operaciones (RF-1..RF-7) ---------------------------------------------------------


def list_datasets(
    exchange: str | None = None,
    market_type: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    *,
    on_progress: ProgressCallback | None = None,
) -> DatasetListResponse:
    """RF-1: lista datasets con filtros opcionales. SI no hay, ENTONCES lista vacía."""
    cfg = _settings()
    started = time.perf_counter()
    datasets = scan_parquet_partitions(cfg.PARQUET_DIR, on_progress=on_progress)
    if exchange:
        datasets = [d for d in datasets if d.id.exchange.lower() == exchange.lower()]
    if market_type:
        datasets = [d for d in datasets if d.id.market_type == market_type.lower()]
    if symbol:
        datasets = [d for d in datasets if d.id.symbol.upper() == symbol.upper()]
    if timeframe:
        datasets = [d for d in datasets if d.id.timeframe == timeframe]

    result = DatasetListResponse(
        datasets=datasets,
        total_count=len(datasets),
        total_size_bytes=sum(d.size_bytes for d in datasets),
        total_rows=sum(d.estimated_rows for d in datasets),
    )
    logger.info(
        "datasets_listed",
        datasets_found=result.total_count,
        scan_duration_ms=int((time.perf_counter() - started) * 1000),
        rows_scanned=result.total_rows,
        bytes_read=result.total_size_bytes,
    )
    return result


def get_summary(identifier: DatasetIdentifier) -> DatasetSummary:
    """RF-2: resumen estadístico (metadatos Parquet + agregados lazy)."""
    cfg = _settings()
    path = resolve_dataset_path(cfg.PARQUET_DIR, identifier)
    with _measure("summary", identifier, path):
        return compute_summary(
            scan_dataset(path),
            identifier,
            metadata=read_dataset_metadata(path),
            gap_threshold_ms=cfg.DATA_VIEWER_GAP_THRESHOLD_MS,
        )


def get_rows(
    identifier: DatasetIdentifier,
    mode: SampleMode = "head",
    n: int = 10,
    cols: list[str] | None = None,
    *,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """RF-3: filas ``head``/``tail``/``sample``.

    ``sample`` se acota a ``FT_DATA_VIEWER_SAMPLE_MAX_ROWS`` (RF-9).
    """
    cfg = _settings()
    if n < 1:
        msg = "n debe ser >= 1"
        raise InvalidArgumentError(msg)
    path = resolve_dataset_path(cfg.PARQUET_DIR, identifier)
    lf = scan_dataset(path)
    _check_columns(lf, cols)
    if mode == "sample" and n > cfg.DATA_VIEWER_SAMPLE_MAX_ROWS:
        logger.warning("sample_size_capped", requested=n, max_rows=cfg.DATA_VIEWER_SAMPLE_MAX_ROWS)
        n = cfg.DATA_VIEWER_SAMPLE_MAX_ROWS
    with _measure("rows", identifier, path) as extra:
        rows = sample_rows(lf, n, mode, cols, seed=seed).to_dicts()
        extra.update(mode=mode, rows_returned=len(rows))
        return rows


def get_gaps(
    identifier: DatasetIdentifier,
    min_gap_candles: int = 1,
) -> list[GapInfo]:
    """RF-4: gaps temporales (umbral ``DATA_VIEWER_GAP_THRESHOLD_MS``)."""
    cfg = _settings()
    if min_gap_candles < 1:
        msg = "min_gap_candles debe ser >= 1"
        raise InvalidArgumentError(msg)
    path = resolve_dataset_path(cfg.PARQUET_DIR, identifier)
    with _measure("gaps", identifier, path) as extra:
        gaps = detect_gaps(
            scan_dataset(path),
            TF_MS_MAP[identifier.timeframe],
            min_gap_candles,
            cfg.DATA_VIEWER_GAP_THRESHOLD_MS,
        )
        extra.update(gaps_found=len(gaps))
        return gaps


def get_anomalies(
    identifier: DatasetIdentifier,
    outlier_method: OutlierMethod = "zscore",
) -> AnomalyReport:
    """RF-5: duplicados, nulos, outliers de precio y volumen anómalo."""
    cfg = _settings()
    path = resolve_dataset_path(cfg.PARQUET_DIR, identifier)
    with _measure("anomalies", identifier, path):
        return detect_anomalies(scan_dataset(path), identifier, outlier_method)


def _guard_output_dir(output_dir: Path, parquet_dir: Path) -> Path:
    """ADR DV-01: el destino nunca puede estar dentro de ``FT_PARQUET_DIR``."""
    target = output_dir.resolve()
    if target.is_relative_to(parquet_dir.resolve()):
        msg = f"El directorio de salida no puede estar dentro de PARQUET_DIR ({parquet_dir})"
        raise InvalidArgumentError(msg)
    return target


def export_file_name(identifier: DatasetIdentifier, fmt: ExportFormat) -> str:
    """``binance_spot_BTCUSDT_1h.csv``."""
    ident = identifier
    return f"{ident.exchange}_{ident.market_type}_{ident.symbol}_{ident.timeframe}.{fmt}"


def export_data(request: ExportRequest, *, stream: IO[bytes] | None = None) -> ExportOutcome:
    """RF-6: exporta un subconjunto filtrado a ``request.output_dir`` o a ``stream``.

    Debe indicarse exactamente uno de los dos destinos.

    Raises:
        InvalidArgumentError: rango invertido, columnas desconocidas, destino ausente/doble
            o dentro de ``PARQUET_DIR`` (ADR DV-01).
        DatasetNotFoundError: SI el dataset no existe.
        NoDataInRangeError: SI el rango no contiene filas.
        OSError: SI el destino no se puede escribir.
    """
    cfg = _settings()
    identifier = normalize_identifier(request.dataset_id)
    start_ms, end_ms, columns = request.start_ms, request.end_ms, request.columns
    if (request.output_dir is None) == (stream is None):
        msg = "Debe indicarse exactamente un destino: output_dir o stream"
        raise InvalidArgumentError(msg)
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        msg = f"start_ms ({start_ms}) debe ser <= end_ms ({end_ms})"
        raise InvalidArgumentError(msg)
    target_dir = (
        _guard_output_dir(Path(request.output_dir), cfg.PARQUET_DIR)
        if request.output_dir is not None
        else None
    )

    path = resolve_dataset_path(cfg.PARQUET_DIR, identifier)
    lf = _filter_range(scan_dataset(path), start_ms, end_ms)
    _check_columns(lf, columns)

    with _measure("export", identifier, path) as extra:
        if int(lf.select(pl.len()).collect().item()) == 0:
            msg = f"No hay datos en el rango solicitado para {dataset_label(identifier)}"
            raise NoDataInRangeError(msg)
        if columns:
            lf = lf.select(columns)
        out_path = target_dir / export_file_name(identifier, request.format) if target_dir else None
        destination: Path | IO[bytes] | None = out_path or stream
        if destination is None:  # inalcanzable: validado al inicio
            msg = "Destino de exportación ausente"
            raise InvalidArgumentError(msg)
        rows = stream_export(lf, request.format, destination)
        extra.update(format=request.format, rows_exported=rows)
        return ExportOutcome(rows=rows, path=out_path)


def _filter_range(lf: pl.LazyFrame, start_ms: int | None, end_ms: int | None) -> pl.LazyFrame:
    if start_ms is not None:
        lf = lf.filter(pl.col(TS_COL) >= start_ms)
    if end_ms is not None:
        lf = lf.filter(pl.col(TS_COL) <= end_ms)
    return lf


def compare_datasets_service(
    dataset_a: DatasetIdentifier,
    dataset_b: DatasetIdentifier,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> CompareResult:
    """RF-7: compara dos datasets (opcionalmente acotados a un rango)."""
    cfg = _settings()
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        msg = f"start_ms ({start_ms}) debe ser <= end_ms ({end_ms})"
        raise InvalidArgumentError(msg)
    path_a = resolve_dataset_path(cfg.PARQUET_DIR, dataset_a)
    path_b = resolve_dataset_path(cfg.PARQUET_DIR, dataset_b)
    with _measure("compare", dataset_a, path_a) as extra:
        extra.update(dataset_b=dataset_label(dataset_b))
        return compare_datasets(
            _filter_range(scan_dataset(path_a), start_ms, end_ms),
            _filter_range(scan_dataset(path_b), start_ms, end_ms),
            dataset_a,
            dataset_b,
        )
