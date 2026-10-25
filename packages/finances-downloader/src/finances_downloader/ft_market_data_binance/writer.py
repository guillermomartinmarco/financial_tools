"""RF-4: escritura Parquet con particionado Hive completo e idempotencia sin pérdida."""

from __future__ import annotations

import uuid
from pathlib import Path

import polars as pl
import structlog
from finances_core.schemas.market_data import OHLCV_SCHEMA

logger = structlog.get_logger(__name__)

#: Claves Hive (en este orden) bajo ``{parquet_dir}/binance/``.
HIVE_KEYS = ["market_type", "symbol", "timeframe", "year", "month"]
#: Columnas físicas del archivo: las claves Hive viven sólo en la ruta.
DATA_COLS = [c for c in OHLCV_SCHEMA if c not in HIVE_KEYS]


def partition_dir(parquet_dir: Path, keys: tuple[str, str, str, int, int]) -> Path:
    """Ruta Hive de ``(market_type, symbol, timeframe, year, month)``."""
    market_type, symbol, timeframe, year, month = keys
    return (
        parquet_dir
        / "binance"
        / f"market_type={market_type}"
        / f"symbol={symbol}"
        / f"timeframe={timeframe}"
        / f"year={year:04d}"
        / f"month={month:02d}"
    )


def _cast_data(df: pl.DataFrame) -> pl.DataFrame:
    return df.select([pl.col(c).cast(OHLCV_SCHEMA[c]) for c in DATA_COLS])


def _merge_partition(output_file: Path, new_rows: pl.DataFrame) -> pl.DataFrame:
    """Combina filas existentes + nuevas; dedup por ``open_time_ms`` (gana la nueva); ordena."""
    new_rows = _cast_data(new_rows)
    if not output_file.exists():
        return new_rows.unique(subset=["open_time_ms"], keep="last", maintain_order=True).sort(
            "open_time_ms"
        )
    existing = _cast_data(pl.read_parquet(output_file))
    return (
        pl.concat([existing, new_rows], how="vertical")
        .unique(subset=["open_time_ms"], keep="last", maintain_order=True)
        .sort("open_time_ms")
    )


def _atomic_write(df: pl.DataFrame, output_file: Path) -> None:
    """Escribe en un temporal del mismo directorio y lo sustituye con ``os.replace``."""
    tmp = output_file.with_name(f".{output_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        df.write_parquet(tmp, compression="zstd", compression_level=3, statistics=True)
        tmp.replace(output_file)  # os.replace: atómico en el mismo volumen
    finally:
        tmp.unlink(missing_ok=True)


def write_parquet_partitioned(df: pl.DataFrame, parquet_dir: Path) -> list[str]:
    """Escribe ``df`` (canónico + ``year``/``month``) en particiones Hive año/mes.

    Para cada partición existente combina sus filas con las nuevas sin descartar
    ninguna fuera del rango descargado, y la reemplaza de forma atómica.

    Returns:
        Rutas de los ``data.parquet`` escritos.
    """
    if df.height == 0:
        logger.warning("write_parquet_empty_dataframe")
        return []

    written: list[str] = []
    for keys, group in df.group_by(HIVE_KEYS, maintain_order=True):
        market_type, symbol, timeframe, year, month = keys
        target_dir = partition_dir(
            parquet_dir,
            (str(market_type), str(symbol), str(timeframe), int(str(year)), int(str(month))),
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        output_file = target_dir / "data.parquet"
        merged = _merge_partition(output_file, group)
        _atomic_write(merged, output_file)
        written.append(str(output_file))
        logger.debug("parquet_partition_written", path=str(output_file), rows=merged.height)

    logger.info(
        "parquet_written", files=len(written), rows=df.height, base=str(parquet_dir / "binance")
    )
    return written
