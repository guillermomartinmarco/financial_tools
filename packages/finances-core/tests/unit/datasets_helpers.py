"""Helpers de tests de ``finances_core.datasets``: almacén Hive sintético y recetas.

Los datos se escriben con el mismo escritor que los descargadores
(:func:`finances_core.marketdata.hive_writer.write_parquet_partitioned`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.datasets import RecipeItem, date_to_ms
from finances_core.schemas.market_data import OHLCV_SCHEMA, TF_MS_MAP
from finances_core.schemas.market_data_yfinance import YF_OHLCV_SCHEMA

PRICE_COLUMNS = ("open", "high", "low", "close")


def ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def make_item(
    exchange: str = "binance",
    *,
    market_type: str | None = None,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    end: date = date(2024, 4, 1),
) -> RecipeItem:
    """Ítem desde 2024-01-01 hasta ``end`` (exclusivo)."""
    default_market = "spot" if exchange == "binance" else "equity"
    return RecipeItem.model_validate(
        {
            "exchange": exchange,
            "market_type": market_type or default_market,
            "symbol": symbol,
            "timeframe": timeframe,
            "start_ms": date_to_ms(date(2024, 1, 1)),
            "end_ms": date_to_ms(end),
        }
    )


def candles(
    item: RecipeItem, start_ms: int | None = None, end_ms: int | None = None
) -> pl.DataFrame:
    """Velas canónicas deterministas del exchange de ``item`` en ``[start_ms, end_ms)``."""
    step = TF_MS_MAP[item.timeframe]
    lo = item.start_ms if start_ms is None else start_ms
    hi = item.end_ms if end_ms is None else end_ms
    times = np.arange(lo, hi, step, dtype=np.int64)
    n = times.size
    base = (100.0 + np.arange(n, dtype=np.float64) * 0.5).astype(np.float32)
    data: dict[str, object] = {
        "symbol": [item.symbol] * n,
        "timeframe": [item.timeframe] * n,
        "market_type": [item.market_type] * n,
        "open_time_ms": times,
        "open": base,
        "high": base + np.float32(1.0),
        "low": base - np.float32(1.0),
        "close": base + np.float32(0.25),
        "volume": np.full(n, 10.0, dtype=np.float32),
    }
    if item.exchange == "binance":
        data |= {
            "quote_volume": np.full(n, 1000.0, dtype=np.float32),
            "trades_count": np.arange(n, dtype=np.int64),
            "taker_buy_base_volume": np.full(n, 5.0, dtype=np.float32),
            "taker_buy_quote_volume": np.full(n, 500.0, dtype=np.float32),
        }
        schema = OHLCV_SCHEMA
    else:
        data["adj_close"] = base
        schema = YF_OHLCV_SCHEMA
    frame = pl.DataFrame(data)
    return frame.select([pl.col(c).cast(t) for c, t in schema.items()])


def write_store(datasets_dir: Path, item: RecipeItem, frame: pl.DataFrame) -> list[str]:
    """Escribe ``frame`` en el layout Hive de ``item`` (modo ``replace`` por mes)."""
    schema = OHLCV_SCHEMA if item.exchange == "binance" else YF_OHLCV_SCHEMA
    dt = pl.from_epoch("open_time_ms", time_unit="ms")
    with_keys = frame.with_columns(
        dt.dt.year().cast(pl.Int32).alias("year"), dt.dt.month().cast(pl.Int32).alias("month")
    )
    return write_parquet_partitioned(
        with_keys,
        datasets_dir,
        exchange=item.exchange,
        schema={**schema, "year": pl.Int32, "month": pl.Int32},
        mode="replace",
    )
