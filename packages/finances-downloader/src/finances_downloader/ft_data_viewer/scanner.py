"""Descubrimiento de datasets y lectura metadata-only (DV-02, DV-04).

Layout leído (spec ``ft_market_data_binance`` RF-4, Hive completo)::

    {PARQUET_DIR}/{exchange}/market_type=X/symbol=Y/timeframe=Z/year=YYYY/month=MM/data.parquet

Los archivos ``data.parquet`` no contienen las columnas de partición; la identidad
del dataset sale exclusivamente de la ruta. Los árboles sin ese layout de velas (p. ej.
``yfinance_events/symbol=Y/events.parquet``) se ignoran en silencio.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args

import polars as pl

# pyarrow no publica stubs ni py.typed; se usa solo para leer el footer Parquet
# (estadísticas por row group), algo que Polars 2.x no expone públicamente.
import pyarrow.parquet as pq
import structlog
from finances_core.schemas.data_viewer import ColumnStats, DatasetIdentifier, DatasetInfo

from finances_downloader.ft_data_viewer.errors import DatasetNotFoundError

logger = structlog.get_logger(__name__, component="ft_data_viewer")

ProgressCallback = Callable[[int, int], None]

#: Valores válidos de ``market_type`` según el contrato ``DatasetIdentifier``.
MARKET_TYPES: tuple[str, ...] = get_args(DatasetIdentifier.model_fields["market_type"].annotation)
#: Texto de ayuda de CLI/API con los ``market_type`` admitidos.
MARKET_HELP = " | ".join(MARKET_TYPES)

_NUMERIC_PREFIXES = ("Int", "UInt", "Float")


def _hive_value(name: str, key: str) -> str | None:
    """Devuelve ``valor`` si ``name`` es ``key=valor`` (partición Hive), si no ``None``."""
    prefix = f"{key}="
    if name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix) :]
    return None


def _hive_children(parent: Path, key: str) -> list[tuple[str, Path]]:
    if not parent.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for child in sorted(parent.iterdir()):
        value = _hive_value(child.name, key) if child.is_dir() else None
        if value is not None:
            out.append((value, child))
    return out


def _discover(parquet_dir: Path) -> list[tuple[DatasetIdentifier, Path]]:
    """Recorre el árbol Hive y devuelve (identificador, raíz del dataset)."""
    found: list[tuple[DatasetIdentifier, Path]] = []
    if not parquet_dir.is_dir():
        return found
    for exchange_dir in sorted(p for p in parquet_dir.iterdir() if p.is_dir()):
        for market_type, market_dir in _hive_children(exchange_dir, "market_type"):
            if market_type not in MARKET_TYPES:
                logger.warning("unknown_market_type_skipped", path=str(market_dir))
                continue
            for symbol, symbol_dir in _hive_children(market_dir, "symbol"):
                for timeframe, tf_dir in _hive_children(symbol_dir, "timeframe"):
                    ident = DatasetIdentifier.model_validate(
                        {
                            "exchange": exchange_dir.name,
                            "market_type": market_type,
                            "symbol": symbol,
                            "timeframe": timeframe,
                        }
                    )
                    found.append((ident, tf_dir))
    return found


def dataset_files(dataset_path: Path) -> list[Path]:
    """Archivos Parquet de un dataset (``year=*/month=*/*.parquet``), en orden estable."""
    return sorted(dataset_path.glob("year=*/month=*/*.parquet"))


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
        year = int(_hive_value(f.parent.parent.name, "year") or 0)
        month = int(_hive_value(f.parent.name, "month") or 0)
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
    candidates = _discover(parquet_dir)
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
    path = (
        base_dir
        / identifier.exchange
        / f"market_type={identifier.market_type}"
        / f"symbol={identifier.symbol}"
        / f"timeframe={identifier.timeframe}"
    )
    inside = path.resolve().is_relative_to(base_dir.resolve())
    if not inside or not path.is_dir() or not dataset_files(path):
        msg = f"Dataset no encontrado: {dataset_label(identifier)}"
        raise DatasetNotFoundError(msg)
    return path


def dataset_label(identifier: DatasetIdentifier) -> str:
    """Identificador legible ``exchange/market_type/symbol/timeframe`` (campo ``dataset_id``)."""
    return "/".join(
        (identifier.exchange, identifier.market_type, identifier.symbol, identifier.timeframe)
    )


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
