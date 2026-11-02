"""Utilidades de los tests de ``import``: archivos de origen y huellas del almacén temporal."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import polars as pl
from finances_core.schemas import ImportRequest

from .conftest import HOUR_MS, JAN_2024_MS

FullFormat = Literal["parquet", "csv", "jsonl"]

#: Esquema canónico de binance (columnas de datos; sin symbol/timeframe/market_type).
BINANCE_SCHEMA: dict[str, type[pl.DataType]] = {
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

YF_SCHEMA: dict[str, type[pl.DataType]] = {
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "adj_close": pl.Float32,
    "volume": pl.Float32,
}


def make_binance(times: Sequence[int], *, close_offset: float = 0.0) -> pl.DataFrame:
    """Velas con el esquema canónico completo de binance."""
    n = len(times)
    return pl.DataFrame(
        {
            "open_time_ms": list(times),
            "open": [100.0 + (t % 5) for t in times],
            "high": [110.0] * n,
            "low": [90.0] * n,
            "close": [100.0 + close_offset + (t % 7) / 10 for t in times],
            "volume": [10.5] * n,
            "quote_volume": [1050.25] * n,
            "trades_count": [100 + (t % 5) for t in times],
            "taker_buy_base_volume": [5.5] * n,
            "taker_buy_quote_volume": [550.75] * n,
        },
        schema=BINANCE_SCHEMA,
    )


def make_yf(times: Sequence[int]) -> pl.DataFrame:
    n = len(times)
    return pl.DataFrame(
        {
            "open_time_ms": list(times),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5 + (t % 3) / 10 for t in times],
            "adj_close": [100.25] * n,
            "volume": [1000.0] * n,
        },
        schema=YF_SCHEMA,
    )


def write_source(
    directory: Path, df: pl.DataFrame, fmt: FullFormat, name: str = "incoming"
) -> Path:
    """Escribe ``df`` como archivo de origen (fuera del almacén) y devuelve su ruta."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.{fmt}"
    if fmt == "parquet":
        df.write_parquet(path)
    elif fmt == "csv":
        df.write_csv(path)
    else:
        df.write_ndjson(path)
    return path


def import_request(source: Path, **overrides: Any) -> ImportRequest:
    """Petición de importación a ``binance/spot/BTCUSDT/1h`` ya confirmada."""
    data: dict[str, Any] = {
        "source": source,
        "exchange": "binance",
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "confirm": True,
    }
    data.update(overrides)
    return ImportRequest.model_validate(data)


def tree_fingerprint(root: Path) -> dict[str, str]:
    """Huella completa del árbol: cada ruta (archivos y directorios) con el hash de su contenido."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        out[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>"
    return out


def read_dataset(root: Path) -> pl.DataFrame:
    """Todas las filas de un dataset Hive (sin columnas de partición), ordenadas por tiempo."""
    files = sorted(root.rglob("*.parquet"))
    frames = [pl.read_parquet(f) for f in files]
    return pl.concat(frames).sort("open_time_ms") if frames else pl.DataFrame()


def hourly(start_ms: int = JAN_2024_MS, n: int = 4) -> list[int]:
    return [start_ms + i * HOUR_MS for i in range(n)]
