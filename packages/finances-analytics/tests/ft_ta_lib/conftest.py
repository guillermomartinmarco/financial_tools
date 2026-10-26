"""Fixtures de ``ft_ta_lib``: reutiliza la fábrica de datasets Hive compartida.

``settings`` y ``hive_dataset`` vienen de ``tests/conftest.py`` (misma fábrica que
``ft_indicators``, en ``tests/datasets.py``); aquí solo se añaden marcos en memoria.
"""

from __future__ import annotations

import polars as pl

from ..datasets import JAN_2024_MS, DatasetFactory, random_walk_ohlcv

HOUR_MS = 3_600_000

__all__ = ["HOUR_MS", "JAN_2024_MS", "DatasetFactory", "ohlcv_frame"]


def ohlcv_frame(rows: int, *, seed: int = 7, adj_close: bool = False) -> pl.DataFrame:
    """Marco canónico en memoria (``open_time_ms`` ``Int64`` + OHLCV ``Float32``)."""
    df = random_walk_ohlcv(rows, seed=seed)
    if adj_close:
        df = df.with_columns((pl.col("close") * 0.98).alias("adj_close"))
    times = pl.Series("open_time_ms", [JAN_2024_MS + i * HOUR_MS for i in range(rows)])
    return (
        df.select(pl.all().cast(pl.Float32))
        .with_columns(times.cast(pl.Int64))
        .select("open_time_ms", *df.columns)
    )
