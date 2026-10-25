from pathlib import Path
from typing import List, Dict
import polars as pl
import structlog
from datetime import datetime

from finances_core.schemas.data_viewer import DatasetIdentifier, DatasetInfo
from finances_core.schemas.market_data import TF_MS_MAP
from finances_core.config import settings

logger = structlog.get_logger(__name__)


def scan_parquet_partitions(parquet_dir: Path | None = None) -> List[DatasetInfo]:
    """
    Escanea recursivamente el directorio Parquet particionado (Hive)
    y retorna lista de DatasetInfo usando metadata-only scan.
    """
    if parquet_dir is None:
        parquet_dir = settings.PARQUET_DIR

    binance_root = parquet_dir / "binance"
    if not binance_root.exists():
        logger.info("parquet_dir_not_found", path=str(binance_root))
        return []

    datasets = []

    # Estructura: binance/{spot|futures}/{SYMBOL}/timeframe={TF}/year={YYYY}/month={MM}/data.parquet
    for market_type_dir in binance_root.iterdir():
        if not market_type_dir.is_dir():
            continue
        # Parsear market_type del nombre del directorio (formato Hive: market_type=spot)
        if market_type_dir.name.startswith("market_type="):
            market_type = market_type_dir.name.split("=")[1]
        else:
            market_type = market_type_dir.name  # compatibilidad con formato anterior

        for symbol_dir in market_type_dir.iterdir():
            if not symbol_dir.is_dir():
                continue
            symbol = symbol_dir.name

            for timeframe_dir in symbol_dir.iterdir():
                if not timeframe_dir.is_dir() or not timeframe_dir.name.startswith("timeframe="):
                    continue
                timeframe = timeframe_dir.name.split("=")[1]

                # Escanear particiones year/month
                year_dirs = [d for d in timeframe_dir.iterdir() if d.is_dir() and d.name.startswith("year=")]
                if not year_dirs:
                    continue

                years = []
                month_count = 0
                estimated_rows = 0
                size_bytes = 0
                last_modified_ms = 0

                for year_dir in year_dirs:
                    year = int(year_dir.name.split("=")[1])
                    years.append(year)
                    for month_dir in year_dir.iterdir():
                        if not month_dir.is_dir() or not month_dir.name.startswith("month="):
                            continue
                        month_count += 1
                        parquet_files = list(month_dir.glob("*.parquet"))
                        for pf in parquet_files:
                            try:
                                stat = pf.stat()
                                size_bytes += stat.st_size
                                if stat.st_mtime > last_modified_ms / 1000:
                                    last_modified_ms = int(stat.st_mtime * 1000)

                                # Leer metadata para estimar rows (usar collect con limit 0 para schema + row count)
                                lf = pl.scan_parquet(pf)
                                try:
                                    # Intentar usar collect_metadata si está disponible (Polars >= 1.0)
                                    meta = lf.collect_metadata()
                                    estimated_rows += meta.num_rows()
                                except AttributeError:
                                    # Fallback: collect con limit 0 para solo schema, luego count rows
                                    count = lf.select(pl.len()).collect()["len"][0]
                                    estimated_rows += count
                            except Exception as e:
                                logger.warning("metadata_read_failed", path=str(pf), error=str(e))

                if not years:
                    continue

                year_range = (min(years), max(years))

                # Path relativo desde FT_PARQUET_DIR
                rel_path = timeframe_dir.relative_to(parquet_dir)

                identifier = DatasetIdentifier(
                    exchange="binance",
                    market_type=market_type,
                    symbol=symbol,
                    timeframe=timeframe,
                )

                datasets.append(DatasetInfo(
                    id=identifier,
                    year_range=year_range,
                    month_count=month_count,
                    estimated_rows=estimated_rows,
                    size_bytes=size_bytes,
                    last_modified_ms=last_modified_ms,
                    partition_path=str(rel_path),
                ))

    logger.info("parquet_scan_complete", datasets_found=len(datasets))
    return datasets


def resolve_dataset_path(base_dir: Path, identifier: "DatasetIdentifier") -> Path:
    """Resuelve la ruta absoluta del dataset dado su identificador.
    Retorna la raíz del dataset particionado (binance/) para que hive_partitioning infiera todas las particiones."""
    return base_dir / "binance"


def read_dataset_metadata(dataset_path: Path) -> Dict[str, Dict]:
    """
    Lee metadata de columnas (min, max, mean, null_count, distinct_count, dtype)
    sin leer datos completos. Usa collect_metadata() de Polars si disponible.
    """
    lf = pl.scan_parquet(dataset_path)
    try:
        meta = lf.collect_metadata()
        result = {}
        for col in meta.schema:
            col_name = col.name
            stats = {}
            if col.statistics:
                stats = {
                    "dtype": str(col.physical_type),
                    "null_count": col.statistics.null_count or 0,
                    "distinct_count": col.statistics.distinct_count,
                    "min": col.statistics.min_value,
                    "max": col.statistics.max_value,
                }
                if col.physical_type in ("FLOAT", "DOUBLE", "INT32", "INT64", "FLOAT32", "FLOAT64"):
                    pass
            result[col_name] = stats
        return result
    except AttributeError:
        # Fallback: usar collect con limit para obtener stats
        return {}