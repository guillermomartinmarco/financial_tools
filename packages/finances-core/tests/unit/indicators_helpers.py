"""Ayudas de tests del núcleo de indicadores: datasets Hive sintéticos y series de salida."""

from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl

from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.market_data import OHLCV_SCHEMA
from finances_core.schemas.market_data_yfinance import YF_OHLCV_SCHEMA

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
#: 2024-01-30T00:00:00Z: 100 velas 1h cruzan de enero a febrero.
JAN_30_2024_MS = 1_706_572_800_000

BINANCE = DatasetIdentifier(
    exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h"
)
YFINANCE = DatasetIdentifier(
    exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1h"
)


def ohlcv_frame(ds: DatasetIdentifier, times: list[int]) -> pl.DataFrame:
    """Frame canónico (con claves Hive) para ``ds`` en los ``open_time_ms`` dados."""
    n = len(times)
    closes = [100.0 + (i % 7) - (i % 3) * 0.5 for i in range(n)]
    df = pl.DataFrame(
        {
            "symbol": [ds.symbol] * n,
            "timeframe": [ds.timeframe] * n,
            "market_type": [ds.market_type] * n,
            "open_time_ms": pl.Series(times, dtype=pl.Int64),
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [10.0 + i for i in range(n)],
        }
    )
    if ds.exchange == "yfinance":
        schema = dict(YF_OHLCV_SCHEMA)
        df = df.with_columns((pl.col("close") * 0.5).alias("adj_close"))
    else:
        schema = dict(OHLCV_SCHEMA)
        df = df.with_columns(
            pl.col("volume").alias("quote_volume"),
            pl.lit(3, dtype=pl.Int64).alias("trades_count"),
            pl.col("volume").alias("taker_buy_base_volume"),
            pl.col("volume").alias("taker_buy_quote_volume"),
        )
    dt = pl.from_epoch(pl.col("open_time_ms"), time_unit="ms")
    return df.select([pl.col(c).cast(t) for c, t in schema.items()]).with_columns(
        dt.dt.year().alias("year"), dt.dt.month().alias("month")
    )


def write_source(
    parquet_dir: Path,
    ds: DatasetIdentifier = BINANCE,
    *,
    rows: int = 100,
    start_ms: int = JAN_30_2024_MS,
    step_ms: int = HOUR_MS,
) -> list[int]:
    """Escribe la fuente Hive de ``ds`` y devuelve sus ``open_time_ms``."""
    times = [start_ms + i * step_ms for i in range(rows)]
    df = ohlcv_frame(ds, times)
    schema = YF_OHLCV_SCHEMA if ds.exchange == "yfinance" else OHLCV_SCHEMA
    write_parquet_partitioned(df, parquet_dir, exchange=ds.exchange, schema=schema)
    return times


def series_frame(times: list[int], column: str, *, offset: float = 0.0) -> pl.DataFrame:
    """Serie de salida ``open_time_ms`` + ``column`` (valores deterministas)."""
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series(times, dtype=pl.Int64),
            column: [None if i < 3 else float(i) + offset for i in range(len(times))],
        }
    )


def tree_digest(root: Path) -> dict[str, str]:
    """``{ruta relativa: sha256}`` de todos los archivos bajo ``root``."""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
