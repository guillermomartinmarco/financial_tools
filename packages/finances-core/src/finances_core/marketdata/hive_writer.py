"""Escritura Parquet con particionado Hive completo e idempotencia sin pérdida.

Layout (``exchange`` lo fija cada herramienta: ``binance``, ``yfinance``, ...)::

    {parquet_dir}/{exchange}/market_type={m}/symbol={S}/timeframe={tf}/year=YYYY/month=MM/data.parquet

Contrato (binance RF-4, yfinance RF-5): merge con lo existente, dedupe por
``open_time_ms`` prevaleciendo la fila nueva, orden ascendente y reemplazo atómico.

``ft_indicators``/``ft_ta_lib`` (T1.1) reutilizan el escritor con otro nombre de archivo
(``file_name="{series_id}.parquet"``) y ``mode="replace"``: cada mes se sustituye por
completo por las filas recibidas (recálculo completo), sin combinar con lo previo.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import polars as pl
import structlog

logger = structlog.get_logger(__name__)

#: Claves Hive (en este orden) bajo ``{parquet_dir}/{exchange}/``.
HIVE_KEYS = ["market_type", "symbol", "timeframe", "year", "month"]

#: Esquema canónico ``{columna: dtype Polars}`` (incluye las claves Hive de la ruta).
ColumnSchema = Mapping[str, pl.DataType | type[pl.DataType]]
PartitionKeys = tuple[str, str, str, int, int]
WriteMode = Literal["merge", "replace"]

#: Nombre del archivo de cada partición de los descargadores.
DEFAULT_FILE_NAME = "data.parquet"
_WRITE_MODES: tuple[WriteMode, ...] = ("merge", "replace")


def data_columns(schema: ColumnSchema) -> list[str]:
    """Columnas físicas del archivo: las claves Hive viven sólo en la ruta."""
    return [c for c in schema if c not in HIVE_KEYS]


def partition_dir(parquet_dir: Path, exchange: str, keys: PartitionKeys) -> Path:
    """Ruta Hive de ``(market_type, symbol, timeframe, year, month)`` bajo ``exchange``."""
    market_type, symbol, timeframe, year, month = keys
    return (
        parquet_dir
        / exchange
        / f"market_type={market_type}"
        / f"symbol={symbol}"
        / f"timeframe={timeframe}"
        / f"year={year:04d}"
        / f"month={month:02d}"
    )


def _cast_data(df: pl.DataFrame, schema: ColumnSchema) -> pl.DataFrame:
    return df.select([pl.col(c).cast(schema[c]) for c in data_columns(schema)])


def merge_partition(
    output_file: Path, new_rows: pl.DataFrame, schema: ColumnSchema
) -> pl.DataFrame:
    """Combina filas existentes + nuevas; dedup por ``open_time_ms`` (gana la nueva); ordena."""
    new_rows = _cast_data(new_rows, schema)
    if not output_file.exists():
        return new_rows.unique(subset=["open_time_ms"], keep="last", maintain_order=True).sort(
            "open_time_ms"
        )
    existing = _cast_data(pl.read_parquet(output_file), schema)
    return (
        pl.concat([existing, new_rows], how="vertical")
        .unique(subset=["open_time_ms"], keep="last", maintain_order=True)
        .sort("open_time_ms")
    )


def replace_partition(new_rows: pl.DataFrame, schema: ColumnSchema) -> pl.DataFrame:
    """Contenido completo del mes en modo ``replace``: solo las filas nuevas, ordenadas."""
    return _cast_data(new_rows, schema).sort("open_time_ms")


def atomic_write(df: pl.DataFrame, output_file: Path) -> None:
    """Escribe en un temporal del mismo directorio y lo sustituye con ``os.replace``."""
    tmp = output_file.with_name(f".{output_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        df.write_parquet(tmp, compression="zstd", compression_level=3, statistics=True)
        tmp.replace(output_file)  # os.replace: atómico en el mismo volumen
    finally:
        tmp.unlink(missing_ok=True)


def write_parquet_partitioned(
    df: pl.DataFrame,
    parquet_dir: Path,
    *,
    exchange: str,
    schema: ColumnSchema,
    file_name: str = DEFAULT_FILE_NAME,
    mode: WriteMode = "merge",
) -> list[str]:
    """Escribe ``df`` (canónico + ``year``/``month``) en particiones Hive año/mes.

    - ``mode="merge"`` (descargadores): combina las filas existentes de cada partición
      con las nuevas sin descartar ninguna fuera del rango descargado.
    - ``mode="replace"`` (indicadores): el archivo del mes contiene exactamente las
      filas recibidas para ese mes; las previas se descartan.

    En ambos modos cada archivo se reemplaza de forma atómica.

    Returns:
        Rutas de los ``file_name`` escritos.

    Raises:
        ValueError: SI ``mode`` no es ``merge`` ni ``replace``.
    """
    if mode not in _WRITE_MODES:
        msg = f"mode inválido: {mode!r} (merge|replace)"
        raise ValueError(msg)
    if df.height == 0:
        logger.warning("write_parquet_empty_dataframe")
        return []

    written: list[str] = []
    for keys, group in df.group_by(HIVE_KEYS, maintain_order=True):
        market_type, symbol, timeframe, year, month = keys
        target_dir = partition_dir(
            parquet_dir,
            exchange,
            (str(market_type), str(symbol), str(timeframe), int(str(year)), int(str(month))),
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        output_file = target_dir / file_name
        if mode == "merge":
            content = merge_partition(output_file, group, schema)
        else:
            content = replace_partition(group, schema)
        atomic_write(content, output_file)
        written.append(str(output_file))
        logger.debug("parquet_partition_written", path=str(output_file), rows=content.height)

    logger.info(
        "parquet_written", files=len(written), rows=df.height, base=str(parquet_dir / exchange)
    )
    return written
