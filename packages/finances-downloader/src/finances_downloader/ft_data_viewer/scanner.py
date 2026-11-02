"""Descubrimiento de datasets y lectura metadata-only (DV-02, DV-04).

El layout Hive de velas y su descubrimiento viven en
``finances_core.marketdata.dataset_layout``, compartidos con ``ft_market_data_manager``
(Art. 4). Este módulo añade la lectura metadata-only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl

# pyarrow no publica stubs ni py.typed; se usa solo para leer el footer Parquet
# (estadísticas por row group), algo que Polars 2.x no expone públicamente.
import pyarrow.parquet as pq
import structlog
from finances_core.marketdata.dataset_layout import (
    MARKET_TYPES,
    dataset_files,
    dataset_label,
    discover_datasets,
    find_dataset_path,
    partition_key,
)
from finances_core.schemas.data_viewer import ColumnStats, DatasetIdentifier, DatasetInfo

from finances_downloader.ft_data_viewer.errors import DatasetNotFoundError

logger = structlog.get_logger(__name__, component="ft_data_viewer")

ProgressCallback = Callable[[int, int], None]

# MARKET_TYPES, dataset_files y dataset_label se reexportan: viven en el core (Art. 4).
__all__ = [
    "MARKET_HELP",
    "MARKET_TYPES",
    "ProgressCallback",
    "dataset_files",
    "dataset_label",
    "dataset_size_bytes",
    "read_dataset_metadata",
    "read_row_count",
    "resolve_dataset_path",
    "scan_dataset",
    "scan_parquet_partitions",
]

#: Texto de ayuda de CLI/API con los ``market_type`` admitidos.
MARKET_HELP = " | ".join(MARKET_TYPES)

_NUMERIC_PREFIXES = ("Int", "UInt", "Float")


def _file_metadata(path: Path) -> Any:
    return pq.read_metadata(path)


def _describe(ident: DatasetIdentifier, root: Path, parquet_dir: Path) -> DatasetInfo | None:
    files = dataset_files(root)
    if not files:
        return None
    years: set[int] = set()
    months: set[tuple[int, int]] = set()
    rows = size = mtime_ms = 0
    for f in files:
        year, month = partition_key(f)
        years.add(year)
        months.add((year, month))
        stat = f.stat()
        size += stat.st_size
        mtime_ms = max(mtime_ms, int(stat.st_mtime * 1000))
        rows += int(_file_metadata(f).num_rows)
    return DatasetInfo(
        id=ident,
        year_range=(min(years), max(years)),
        month_count=len(months),
        estimated_rows=rows,
        size_bytes=size,
        last_modified_ms=mtime_ms,
        partition_path=root.relative_to(parquet_dir).as_posix(),
    )


def scan_parquet_partitions(
    parquet_dir: Path, on_progress: ProgressCallback | None = None
) -> list[DatasetInfo]:
    """Escanea recursivamente ``parquet_dir`` y devuelve un ``DatasetInfo`` por dataset.

    Solo se leen rutas y footers Parquet (``num_rows``); nunca filas (DV-04).
    SI no hay datasets, ENTONCES devuelve lista vacía (RF-1).
    """
    started = time.perf_counter()
    candidates = discover_datasets(parquet_dir)
    infos: list[DatasetInfo] = []
    for done, (ident, root) in enumerate(candidates, start=1):
        try:
            info = _describe(ident, root, parquet_dir)
        except OSError as exc:
            logger.warning("dataset_metadata_unreadable", path=str(root), error=str(exc))
            info = None
        if info is not None:
            infos.append(info)
        if on_progress is not None:
            on_progress(done, len(candidates))
    logger.debug(
        "parquet_scan_complete",
        datasets_found=len(infos),
        scan_duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return infos


def resolve_dataset_path(base_dir: Path, identifier: DatasetIdentifier) -> Path:
    """Resuelve la raíz Hive del dataset concreto.

    Raises:
        DatasetNotFoundError: SI la ruta no existe, no contiene Parquet o escapa de ``base_dir``.
    """
    path = find_dataset_path(base_dir, identifier)
    if path is None:
        msg = f"Dataset no encontrado: {dataset_label(identifier)}"
        raise DatasetNotFoundError(msg)
    return path


def scan_dataset(dataset_path: Path) -> pl.LazyFrame:
    """LazyFrame del dataset sin columnas de partición (``year``/``month``)."""
    return pl.scan_parquet(dataset_files(dataset_path), hive_partitioning=False)


def read_row_count(dataset_path: Path) -> int:
    """Número total de filas leído de los footers Parquet (sin leer filas)."""
    return sum(int(_file_metadata(f).num_rows) for f in dataset_files(dataset_path))


def dataset_size_bytes(dataset_path: Path) -> int:
    """Bytes en disco del dataset (métrica ``bytes_read`` de RF-8)."""
    return sum(f.stat().st_size for f in dataset_files(dataset_path))


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def read_dataset_metadata(dataset_path: Path) -> dict[str, ColumnStats]:
    """Estadísticas por columna a partir de los footers Parquet (metadata-only, DV-04).

    Agrega todos los row groups de todos los archivos: ``min``/``max`` numéricos y
    ``null_count``. ``mean``/``std``/``distinct_count`` no son agregables desde las
    estadísticas Parquet y se dejan en ``None`` (los completa ``compute_summary``).
    SI algún row group carece de estadísticas, ENTONCES ``min``/``max`` quedan en ``None``.
    """
    files = dataset_files(dataset_path)
    if not files:
        return {}
    schema = pl.read_parquet_schema(files[0])
    acc: dict[str, dict[str, Any]] = {
        name: {"min": None, "max": None, "nulls": 0, "complete": True} for name in schema
    }
    for f in files:
        meta = _file_metadata(f)
        for rg in range(int(meta.num_row_groups)):
            group = meta.row_group(rg)
            for ci in range(int(group.num_columns)):
                col = group.column(ci)
                name = str(col.path_in_schema)
                slot = acc.get(name)
                if slot is None:
                    continue
                stats = col.statistics
                if stats is None:
                    slot["complete"] = False
                    continue
                slot["nulls"] += int(stats.null_count or 0)
                if not stats.has_min_max:
                    slot["complete"] = False
                    continue
                lo, hi = _as_float(stats.min), _as_float(stats.max)
                if lo is None or hi is None:
                    slot["complete"] = False
                    continue
                slot["min"] = lo if slot["min"] is None else min(slot["min"], lo)
                slot["max"] = hi if slot["max"] is None else max(slot["max"], hi)
    result: dict[str, ColumnStats] = {}
    for name, dtype in schema.items():
        slot = acc[name]
        numeric = str(dtype).startswith(_NUMERIC_PREFIXES) and slot["complete"]
        result[name] = ColumnStats(
            name=name,
            dtype=str(dtype),
            null_count=slot["nulls"],
            min=slot["min"] if numeric else None,
            max=slot["max"] if numeric else None,
        )
    return result
