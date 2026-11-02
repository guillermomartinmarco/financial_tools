"""Fixtures de ft_market_data_manager: almacén Parquet Hive real y temporal (sin mocks).

Los datasets se construyen con Polars en el layout RF-4 (Art. 4: sin importar ninguna
``ft_*``). ``write_partition_files`` permite varios archivos por partición mensual.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import polars as pl
import pytest
import structlog
from finances_core.config import FinancesSettings

HOUR_MS = 3_600_000
#: 2024-01-01T00:00:00Z
JAN_2024_MS = 1_704_067_200_000
#: 2024-02-01T00:00:00Z
FEB_2024_MS = 1_706_745_600_000
#: 2024-03-01T00:00:00Z
MAR_2024_MS = 1_709_251_200_000

FILE_SCHEMA: dict[str, type[pl.DataType]] = {
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "volume": pl.Float32,
    "trades_count": pl.Int64,
}


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    structlog.reset_defaults()


def make_ohlcv(times: Sequence[int], *, close_offset: float = 0.0) -> pl.DataFrame:
    """Velas con los ``open_time_ms`` dados (en ese orden); ``close_offset`` marca la versión."""
    return pl.DataFrame(
        {
            "open_time_ms": list(times),
            "open": [100.0 for _ in times],
            "high": [101.0 for _ in times],
            "low": [99.0 for _ in times],
            "close": [100.0 + close_offset + (t % 7) / 10 for t in times],
            "volume": [10.0 for _ in times],
            "trades_count": [100 + (t % 5) for t in times],
        },
        schema=FILE_SCHEMA,
    )


def hours(start_ms: int, n: int) -> list[int]:
    return [start_ms + i * HOUR_MS for i in range(n)]


def dataset_root(
    parquet_dir: Path,
    *,
    exchange: str = "binance",
    market_type: str = "spot",
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
) -> Path:
    return (
        parquet_dir
        / exchange
        / f"market_type={market_type}"
        / f"symbol={symbol}"
        / f"timeframe={timeframe}"
    )


def write_partition_files(
    parquet_dir: Path,
    year: int,
    month: int,
    frames: Sequence[pl.DataFrame],
    *,
    names: Sequence[str] | None = None,
    exchange: str = "binance",
    market_type: str = "spot",
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
) -> list[Path]:
    """Escribe un archivo por frame en ``year=YYYY/month=MM`` (nombres lexicográficos)."""
    root = dataset_root(
        parquet_dir,
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        timeframe=timeframe,
    )
    target = root / f"year={year:04d}" / f"month={month:02d}"
    target.mkdir(parents=True, exist_ok=True)
    file_names = list(names) if names else [f"part-{i:03d}.parquet" for i in range(len(frames))]
    paths: list[Path] = []
    for name, frame in zip(file_names, frames, strict=True):
        path = target / name
        frame.write_parquet(path)
        paths.append(path)
    return paths


@pytest.fixture
def parquet_dir(tmp_path: Path) -> Path:
    """``PARQUET_DIR`` efectivo de ``FinancesSettings`` (FT_PROJECT_ROOT=tmp_path)."""
    path = FinancesSettings().PARQUET_DIR
    assert path.is_relative_to(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path
