"""Fixtures de ft_data_viewer: datasets Parquet sintéticos en el layout RF-4 (Hive completo).

Los datasets se construyen directamente con Polars (Art. 4: sin importar
``ft_market_data_binance``). Cada archivo ``data.parquet`` contiene las columnas
canónicas sin las columnas de partición, igual que el writer real.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from finances_core.config import FinancesSettings

HOUR_MS = 3_600_000
#: 2024-01-01T00:00:00Z
JAN_2024_MS = 1_704_067_200_000

FILE_SCHEMA: dict[str, type[pl.DataType]] = {
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "volume": pl.Float32,
    "quote_volume": pl.Float32,
    "trades_count": pl.Int64,
    "taker_buy_base_volume": pl.Float32,
    "taker_buy_quote_volume": pl.Float32,
}


def make_ohlcv(
    n: int,
    *,
    start_ms: int = JAN_2024_MS,
    tf_ms: int = HOUR_MS,
    drop: Sequence[int] = (),
    price: float = 100.0,
) -> pl.DataFrame:
    """Genera ``n`` velas regulares (precio con ruido determinista) omitiendo índices ``drop``."""
    dropped = set(drop)
    rows = [i for i in range(n) if i not in dropped]
    noise = [((i * 7919) % 13 - 6) / 10 for i in rows]  # [-0.6, 0.6]
    close = [price + x for x in noise]
    return pl.DataFrame(
        {
            "open_time_ms": [start_ms + i * tf_ms for i in rows],
            "open": [c - 0.1 for c in close],
            "high": [c + 0.5 for c in close],
            "low": [c - 0.5 for c in close],
            "close": close,
            "volume": [10.0 + (i % 5) for i in rows],
            "quote_volume": [1000.0 + i for i in rows],
            "trades_count": [100 + i for i in rows],
            "taker_buy_base_volume": [5.0 for _ in rows],
            "taker_buy_quote_volume": [500.0 for _ in rows],
        },
        schema=FILE_SCHEMA,
    )


def write_dataset(
    parquet_dir: Path,
    df: pl.DataFrame,
    *,
    exchange: str = "binance",
    market_type: str = "spot",
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
) -> Path:
    """Escribe ``df`` particionado por año/mes en el layout RF-4 y devuelve la raíz del dataset."""
    root = (
        parquet_dir
        / exchange
        / f"market_type={market_type}"
        / f"symbol={symbol}"
        / f"timeframe={timeframe}"
    )
    keyed = df.with_columns(
        pl.col("open_time_ms")
        .map_elements(lambda ms: datetime.fromtimestamp(ms / 1000, tz=UTC).year, pl.Int32)
        .alias("_y"),
        pl.col("open_time_ms")
        .map_elements(lambda ms: datetime.fromtimestamp(ms / 1000, tz=UTC).month, pl.Int32)
        .alias("_m"),
    )
    for (year, month), part in keyed.group_by(["_y", "_m"], maintain_order=True):
        target = root / f"year={year}" / f"month={month:02d}"
        target.mkdir(parents=True, exist_ok=True)
        part.drop("_y", "_m").write_parquet(target / "data.parquet")
    return root


@pytest.fixture
def parquet_dir(tmp_path: Path) -> Path:
    """``PARQUET_DIR`` efectivo de ``FinancesSettings`` (FT_PROJECT_ROOT=tmp_path)."""
    path = FinancesSettings().PARQUET_DIR
    assert path.is_relative_to(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def btc_dataset(parquet_dir: Path) -> Path:
    """binance/spot/BTCUSDT/1h: 48 velas desde 2024-01-31 00:00 (cruza enero→febrero), sin gaps."""
    start = JAN_2024_MS + 30 * 24 * HOUR_MS
    return write_dataset(parquet_dir, make_ohlcv(48, start_ms=start))
