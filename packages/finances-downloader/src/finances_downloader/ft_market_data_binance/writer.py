"""RF-4: escritura Parquet con particionado Hive completo e idempotencia sin pérdida.

Adaptador fino sobre :mod:`finances_core.marketdata.hive_writer` con
``exchange="binance"`` y el esquema canónico ``OHLCV_SCHEMA``.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from finances_core.marketdata import hive_writer
from finances_core.marketdata.hive_writer import HIVE_KEYS, PartitionKeys
from finances_core.schemas.market_data import OHLCV_SCHEMA

EXCHANGE = "binance"
#: Columnas físicas del archivo: las claves Hive viven sólo en la ruta.
DATA_COLS = hive_writer.data_columns(OHLCV_SCHEMA)

__all__ = ["DATA_COLS", "EXCHANGE", "HIVE_KEYS", "partition_dir", "write_parquet_partitioned"]


def partition_dir(parquet_dir: Path, keys: PartitionKeys) -> Path:
    """Ruta Hive de ``(market_type, symbol, timeframe, year, month)`` bajo ``binance/``."""
    return hive_writer.partition_dir(parquet_dir, EXCHANGE, keys)


def write_parquet_partitioned(df: pl.DataFrame, parquet_dir: Path) -> list[str]:
    """Escribe ``df`` (canónico + ``year``/``month``) en particiones Hive año/mes de Binance.

    Returns:
        Rutas de los ``data.parquet`` escritos.
    """
    return hive_writer.write_parquet_partitioned(
        df, parquet_dir, exchange=EXCHANGE, schema=OHLCV_SCHEMA
    )
