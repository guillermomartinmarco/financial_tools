"""Lectura del almacén Hive de recetas por ítem (plan §3.4).

Layout (el de los descargadores, :mod:`finances_core.marketdata.hive_writer`)::

    {datasets_dir}/{exchange}/market_type={mt}/symbol={sym}/timeframe={tf}/year=YYYY/month=MM/data.parquet

Solo se leen las particiones mensuales que solapan ``[start_ms, end_ms)`` y solo las
columnas estrictas e informativas del exchange. Este módulo nunca escribe; el único
borrado (``--refetch``) se limita a ``datasets_dir``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import structlog

from finances_core.datasets.errors import DatasetDataError
from finances_core.datasets.hashing import INFORMATIONAL_COLUMNS, STRICT_COLUMNS, check_chunk
from finances_core.indicators_store.source import SOURCE_FILE_NAME, TIME_COLUMN
from finances_core.marketdata.inventory import partition_base_dir
from finances_core.marketdata.months import get_months_in_range
from finances_core.schemas.datasets import RecipeItem

logger = structlog.get_logger(__name__)


def item_columns(item: RecipeItem) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(estrictas, informativas)`` del exchange del ítem (RF-3)."""
    return STRICT_COLUMNS[item.exchange], INFORMATIONAL_COLUMNS[item.exchange]


def item_dir(datasets_dir: Path, item: RecipeItem) -> Path:
    """Directorio Hive ``{exchange}/market_type=…/symbol=…/timeframe=…`` del ítem."""
    return partition_base_dir(
        datasets_dir, item.exchange, item.symbol, item.timeframe, item.market_type
    )


def item_months(item: RecipeItem) -> list[tuple[int, int]]:
    """Meses UTC ``(año, mes)`` que solapan ``[start_ms, end_ms)``."""
    return get_months_in_range(item.start_ms, item.end_ms - 1)


def _month_dir(base: Path, year: int, month: int) -> Path:
    return base / f"year={year:04d}" / f"month={month:02d}"


def item_files(datasets_dir: Path, item: RecipeItem) -> list[tuple[str, Path]]:
    """``("YYYY-MM", archivo)`` de los meses que solapan el ítem y existen, en orden."""
    base = item_dir(datasets_dir, item)
    out: list[tuple[str, Path]] = []
    for year, month in item_months(item):
        path = _month_dir(base, year, month) / SOURCE_FILE_NAME
        if path.is_file():
            out.append((f"{year:04d}-{month:02d}", path))
    return out


def _range_filter(item: RecipeItem) -> pl.Expr:
    return (pl.col(TIME_COLUMN) >= item.start_ms) & (pl.col(TIME_COLUMN) < item.end_ms)


def scan_item(datasets_dir: Path, item: RecipeItem) -> Iterator[tuple[str, pl.DataFrame]]:
    """Filas del ítem mes a mes (solo meses con filas en el rango), validadas.

    Cada ``DataFrame`` contiene las columnas estrictas seguidas de las informativas.

    Raises:
        DatasetDataError: columnas ausentes, dtype no canónico, ``open_time_ms`` nulo,
            duplicado o desordenado (también entre meses), o archivo ilegible.
        OSError: error de IO al leer el almacén.
    """
    strict, informational = item_columns(item)
    columns = [*strict, *informational]
    previous_last: int | None = None
    for month, path in item_files(datasets_dir, item):
        try:
            lf = pl.scan_parquet(path, hive_partitioning=False)
            present = lf.collect_schema()
            check_chunk(pl.DataFrame(schema=present), columns, None)
            frame = lf.select(columns).filter(_range_filter(item)).collect()
        except pl.exceptions.PolarsError as exc:
            msg = f"Partición ilegible {path}: {exc}"
            raise DatasetDataError(msg) from exc
        check_chunk(frame, columns, previous_last)
        if frame.height == 0:
            continue
        previous_last = int(frame[TIME_COLUMN][-1])
        yield month, frame


def count_rows(datasets_dir: Path, item: RecipeItem) -> int:
    """Filas del ítem en ``[start_ms, end_ms)`` (solo ``open_time_ms``; sin validar)."""
    files = [path for _, path in item_files(datasets_dir, item)]
    if not files:
        return 0
    try:
        counted = (
            pl.scan_parquet(files, hive_partitioning=False)
            .select(TIME_COLUMN)
            .filter(_range_filter(item))
            .select(pl.len())
            .collect()
        )
    except pl.exceptions.PolarsError as exc:
        msg = f"Particiones ilegibles de {item.key}: {exc}"
        raise DatasetDataError(msg) from exc
    return int(counted.item())


def scan_item_lazy(datasets_dir: Path, item: RecipeItem) -> pl.LazyFrame:
    """``LazyFrame`` de todas las columnas del ítem filtrado a ``[start_ms, end_ms)``.

    Raises:
        DatasetDataError: SI el ítem no tiene ninguna partición.
    """
    files = [path for _, path in item_files(datasets_dir, item)]
    if not files:
        msg = f"Sin particiones para {item.key} en {datasets_dir}"
        raise DatasetDataError(msg)
    return pl.scan_parquet(files, hive_partitioning=False).filter(_range_filter(item))


def delete_item_partitions(datasets_dir: Path, item: RecipeItem) -> list[Path]:
    """Borra los directorios mensuales que solapan el ítem (``--refetch``, RF-4).

    Nunca borra fuera de ``datasets_dir`` ni meses que no solapan el rango.

    Returns:
        Directorios ``month=MM`` borrados.

    Raises:
        DatasetDataError: SI algún directorio a borrar queda fuera de ``datasets_dir``
            (no se borra nada).
    """
    root = datasets_dir.resolve()
    base = item_dir(datasets_dir, item)
    targets = [
        _month_dir(base, year, month)
        for year, month in item_months(item)
        if _month_dir(base, year, month).is_dir()
    ]
    outside = [t for t in targets if not t.resolve().is_relative_to(root)]
    if outside:
        msg = f"Borrado rechazado: {[str(t) for t in outside]} fuera de {root}"
        raise DatasetDataError(msg)
    for target in targets:
        shutil.rmtree(target)
        logger.info("dataset_partition_deleted", item=item.key, path=str(target))
    return targets
