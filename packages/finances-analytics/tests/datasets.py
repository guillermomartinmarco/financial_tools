"""Fábrica de datasets Hive sintéticos compartida por ``ft_indicators`` y ``ft_ta_lib``.

Binance y yfinance, con y sin ``adj_close``. Las *fixtures* ``settings`` y ``hive_dataset``
viven en ``tests/conftest.py``; este módulo solo contiene código de test (nunca de ``src``).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Protocol

import polars as pl
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.market_data import OHLCV_SCHEMA, TF_MS_MAP
from finances_core.schemas.market_data_yfinance import YF_OHLCV_SCHEMA

#: 2024-01-01T00:00:00Z en ms.
JAN_2024_MS = 1_704_067_200_000


class DatasetFactory(Protocol):
    def __call__(
        self,
        *,
        exchange: str = ...,
        market_type: str = ...,
        symbol: str = ...,
        timeframe: str = ...,
        rows: int = ...,
        start_ms: int = ...,
        seed: int = ...,
    ) -> DatasetIdentifier: ...


def random_walk_ohlcv(n: int, *, seed: int = 7, start_price: float = 100.0) -> pl.DataFrame:
    """OHLCV determinista (paseo aleatorio con semilla fija)."""
    rng = random.Random(seed)
    closes: list[float] = []
    price = start_price
    for _ in range(n):
        price = max(0.01, price * (1.0 + rng.gauss(0.0, 0.01)))
        closes.append(price)
    opens = [start_price, *closes[:-1]]
    highs = [
        max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.003))) for o, c in zip(opens, closes, strict=True)
    ]
    lows = [
        min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.003))) for o, c in zip(opens, closes, strict=True)
    ]
    volumes = [abs(rng.gauss(1000.0, 200.0)) for _ in range(n)]
    return pl.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    )


def write_hive_dataset(
    parquet_dir: Path,
    *,
    exchange: str = "binance",
    market_type: str = "spot",
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    rows: int = 100,
    start_ms: int = JAN_2024_MS,
    seed: int = 7,
) -> DatasetIdentifier:
    """Escribe un dataset canónico en ``parquet_dir`` con el escritor Hive del núcleo.

    ``exchange="yfinance"`` usa ``YF_OHLCV_SCHEMA`` (con ``adj_close``); cualquier otro
    exchange usa el esquema de Binance (sin ``adj_close``).
    """
    step = TF_MS_MAP[timeframe]
    ts = pl.Series("open_time_ms", [start_ms + i * step for i in range(rows)], dtype=pl.Int64)
    df = random_walk_ohlcv(rows, seed=seed).with_columns(
        ts,
        pl.lit(symbol).alias("symbol"),
        pl.lit(timeframe).alias("timeframe"),
        pl.lit(market_type).alias("market_type"),
    )
    schema: dict[str, pl.DataType | type[pl.DataType]]
    if exchange == "yfinance":
        schema = dict(YF_OHLCV_SCHEMA)
        df = df.with_columns((pl.col("close") * 0.98).alias("adj_close"))
    else:
        schema = dict(OHLCV_SCHEMA)
        df = df.with_columns(
            (pl.col("volume") * pl.col("close")).alias("quote_volume"),
            pl.lit(10, dtype=pl.Int64).alias("trades_count"),
            (pl.col("volume") / 2).alias("taker_buy_base_volume"),
            (pl.col("volume") * pl.col("close") / 2).alias("taker_buy_quote_volume"),
        )
    dt = pl.from_epoch(pl.col("open_time_ms"), time_unit="ms")
    df = df.select([pl.col(c).cast(t) for c, t in schema.items()]).with_columns(
        dt.dt.year().alias("year"), dt.dt.month().alias("month")
    )
    write_parquet_partitioned(df, parquet_dir, exchange=exchange, schema=schema)
    return DatasetIdentifier.model_validate(
        {"exchange": exchange, "market_type": market_type, "symbol": symbol, "timeframe": timeframe}
    )
