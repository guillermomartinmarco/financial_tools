"""Normalización canónica RF-4 de un :class:`YahooBatch` a ``YF_OHLCV_SCHEMA`` (Polars)."""

from __future__ import annotations

import polars as pl
import structlog
from finances_core.schemas.market_data_yfinance import INSTRUMENT_TYPE_MAP, YF_OHLCV_SCHEMA

from finances_downloader.ft_market_data_yfinance.adapter import YahooBatch
from finances_downloader.ft_market_data_yfinance.timeframes import max_candle_ms

logger = structlog.get_logger(__name__)

UNKNOWN_MARKET_TYPE = "other"
PRICE_COLUMNS = ("open", "high", "low", "close")

#: Columnas de salida: esquema canónico + claves de partición ``year``/``month``.
OUTPUT_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    **YF_OHLCV_SCHEMA,
    "year": pl.Int32,
    "month": pl.Int32,
}


def resolve_market_type(instrument_type: str | None, symbol: str) -> str:
    """``instrumentType`` de Yahoo -> ``market_type``; desconocido -> ``other`` + warning."""
    if instrument_type is not None and instrument_type in INSTRUMENT_TYPE_MAP:
        return INSTRUMENT_TYPE_MAP[instrument_type]
    logger.warning("unknown_instrument_type", symbol=symbol, instrument_type=instrument_type)
    return UNKNOWN_MARKET_TYPE


def normalize_yahoo_batch(
    batch: YahooBatch,
    *,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    now_ms: int | None = None,
) -> pl.DataFrame:
    """Lote de Yahoo -> velas canónicas listas para ``write_parquet_partitioned``.

    - Filtra ``open_time_ms`` a ``[start_ms, end_ms)``.
    - Con ``now_ms``, descarta la vela aún en formación (``open + duración > now``)
      para que la re-ejecución la pida completa en lugar de congelar un parcial.
    - Descarta filas con OHLC nulo; dedupe por ``open_time_ms`` (gana la última); ordena.
    - Precios ``float32`` crudos, ``adj_close`` y ``volume`` ``float32``; ``year``/``month``.
    """
    if batch.candles.height == 0:
        return pl.DataFrame(schema=OUTPUT_SCHEMA)

    market_type = resolve_market_type(batch.instrument_type, symbol)
    df = batch.candles.filter(
        (pl.col("open_time_ms") >= start_ms) & (pl.col("open_time_ms") < end_ms)
    )
    if now_ms is not None:
        df = df.filter(pl.col("open_time_ms") + max_candle_ms(timeframe) <= now_ms)

    opened = pl.from_epoch(pl.col("open_time_ms"), time_unit="ms")
    return (
        df.drop_nulls(subset=list(PRICE_COLUMNS))
        .unique(subset=["open_time_ms"], keep="last", maintain_order=True)
        .sort("open_time_ms")
        .with_columns(
            pl.lit(symbol).alias("symbol"),
            pl.lit(timeframe).alias("timeframe"),
            pl.lit(market_type).alias("market_type"),
            opened.dt.year().cast(pl.Int32).alias("year"),
            opened.dt.month().cast(pl.Int32).alias("month"),
        )
        .select([pl.col(name).cast(dtype) for name, dtype in OUTPUT_SCHEMA.items()])
    )
