"""Tabla de eventos corporativos sin pérdida (RF-5, ADR-Y03).

Layout: ``{FT_PARQUET_DIR}/yfinance_events/symbol={SYMBOL}/events.parquet`` con
``event_time_ms`` (int64), ``event_type`` (``dividend`` | ``split`` | ``capital_gain``)
y ``value`` (float64). Merge con lo existente, dedupe por
``(event_time_ms, event_type)`` prevaleciendo lo nuevo y escritura atómica.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import structlog
from finances_core.marketdata.hive_writer import atomic_write
from finances_core.schemas.market_data_yfinance import YF_EVENTS_SCHEMA

logger = structlog.get_logger(__name__)

EVENTS_DIR = "yfinance_events"
EVENTS_FILE = "events.parquet"
EVENT_KEY = ["event_time_ms", "event_type"]


def events_file(parquet_dir: Path, symbol: str) -> Path:
    """Ruta del archivo de eventos de ``symbol`` (tal cual Yahoo)."""
    return parquet_dir / EVENTS_DIR / f"symbol={symbol}" / EVENTS_FILE


def _cast(df: pl.DataFrame) -> pl.DataFrame:
    return df.select([pl.col(name).cast(dtype) for name, dtype in YF_EVENTS_SCHEMA.items()])


def write_events(events: pl.DataFrame, parquet_dir: Path, symbol: str) -> str | None:
    """Combina ``events`` con el archivo del símbolo y lo reemplaza atómicamente.

    Returns:
        Ruta escrita, o ``None`` si el lote no trae eventos (el archivo no se crea ni se toca).
    """
    if events.height == 0:
        return None
    output = events_file(parquet_dir, symbol)
    output.parent.mkdir(parents=True, exist_ok=True)
    new_rows = _cast(events)
    combined = (
        pl.concat([_cast(pl.read_parquet(output)), new_rows], how="vertical")
        if output.exists()
        else new_rows
    )
    merged = combined.unique(subset=EVENT_KEY, keep="last", maintain_order=True).sort(EVENT_KEY)
    atomic_write(merged, output)
    logger.info("events_written", symbol=symbol, path=str(output), rows=merged.height)
    return str(output)
