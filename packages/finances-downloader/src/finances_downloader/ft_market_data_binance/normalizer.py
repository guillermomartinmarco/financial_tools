"""RF-3: normalización inmediata de klines Binance al esquema canónico (Art. 5)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Literal

import polars as pl
import structlog
from finances_core.schemas.market_data import OHLCV_SCHEMA

logger = structlog.get_logger(__name__)

#: Columnas crudas de Binance (idénticas en CSV de Public Data y en el array JSON de REST).
RAW_COLS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
]
PUBLIC_DATA_COLS = RAW_COLS
REST_COLS = RAW_COLS

_RENAME = {
    "open_time": "open_time_ms",
    "quote_asset_volume": "quote_volume",
    "number_of_trades": "trades_count",
    "taker_buy_base_asset_volume": "taker_buy_base_volume",
    "taker_buy_quote_asset_volume": "taker_buy_quote_volume",
}

#: Por encima de este valor un timestamp está en microsegundos (Spot Public Data desde 2025).
MICROSECONDS_THRESHOLD = 10**14
PARTITION_COLS = ["year", "month"]
OUTPUT_COLS = [*OHLCV_SCHEMA, *PARTITION_COLS]


def normalize_binance_klines(
    df: pl.DataFrame,
    source: Literal["public_data", "rest_api"],
    *,
    symbol: str,
    timeframe: str,
    market_type: str,
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    """Normaliza klines crudos (12 columnas Binance) al esquema canónico + ``year``/``month``.

    Pasos: renombrar, castear a ``Int64``/``Float32``, unificar timestamps a ms,
    filtrar ``[start_ms, end_ms]``, deduplicar por ``open_time_ms`` (gana la
    última), ordenar y añadir columnas de partición.
    """
    missing = set(RAW_COLS) - set(df.columns)
    if missing:
        msg = f"Faltan columnas Binance ({source}): {sorted(missing)}"
        raise ValueError(msg)

    open_time = pl.col("open_time_ms").cast(pl.Int64)
    out = (
        df.lazy()
        .rename(_RENAME)
        .with_columns(
            pl.when(open_time >= MICROSECONDS_THRESHOLD)
            .then(open_time // 1000)
            .otherwise(open_time)
            .alias("open_time_ms"),
            *[
                pl.col(c).cast(dtype)
                for c, dtype in OHLCV_SCHEMA.items()
                if dtype != pl.Utf8 and c != "open_time_ms"
            ],
            pl.lit(symbol.upper(), dtype=pl.Utf8).alias("symbol"),
            pl.lit(timeframe, dtype=pl.Utf8).alias("timeframe"),
            pl.lit(market_type, dtype=pl.Utf8).alias("market_type"),
        )
        .filter(pl.col("open_time_ms").is_between(start_ms, end_ms))
        .unique(subset=["open_time_ms"], keep="last", maintain_order=True)
        .sort("open_time_ms")
        .with_columns(
            pl.from_epoch("open_time_ms", time_unit="ms").dt.year().cast(pl.Int32).alias("year"),
            pl.from_epoch("open_time_ms", time_unit="ms").dt.month().cast(pl.Int32).alias("month"),
        )
        .select(OUTPUT_COLS)
        .collect()
    )

    for col, dtype in OHLCV_SCHEMA.items():
        if out.schema[col] != dtype:  # pragma: no cover - defensivo, los casts lo garantizan
            msg = f"dtype inválido en {col}: {out.schema[col]} (esperado {dtype})"
            raise ValueError(msg)
    return out


def _read_raw_csv(data: bytes) -> pl.DataFrame:
    """Lee un CSV de klines detectando cabecera (Futures sí, Spot no)."""
    first = data.lstrip()[:1]
    has_header = bool(first) and not first.isdigit()
    return pl.read_csv(
        io.BytesIO(data),
        has_header=has_header,
        new_columns=RAW_COLS,
        schema_overrides=dict.fromkeys(RAW_COLS, pl.Utf8),
    )


def read_public_data_csv(
    csv_path: str | Path,
    symbol: str,
    timeframe: str,
    market_type: str,
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    """Lee un CSV de Public Data y lo normaliza."""
    raw = _read_raw_csv(Path(csv_path).read_bytes())
    return normalize_binance_klines(
        raw,
        "public_data",
        symbol=symbol,
        timeframe=timeframe,
        market_type=market_type,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def read_public_data_zip(
    zip_path: Path,
    symbol: str,
    timeframe: str,
    market_type: str,
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    """Lee el CSV contenido en un .zip mensual de Public Data (sin extraer a disco)."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            msg = f"El zip {zip_path.name} no contiene ningún CSV"
            raise ValueError(msg)
        data = zf.read(names[0])
    raw = _read_raw_csv(data)
    return normalize_binance_klines(
        raw,
        "public_data",
        symbol=symbol,
        timeframe=timeframe,
        market_type=market_type,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def normalize_rest_json(
    json_data: list[list[Any]],
    symbol: str,
    timeframe: str,
    market_type: str,
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    """Normaliza la respuesta JSON (arrays de 12 elementos) de ``/klines``."""
    rows = [[str(v) for v in row[: len(RAW_COLS)]] for row in json_data]
    raw = pl.DataFrame(rows, schema=dict.fromkeys(RAW_COLS, pl.Utf8), orient="row")
    return normalize_binance_klines(
        raw,
        "rest_api",
        symbol=symbol,
        timeframe=timeframe,
        market_type=market_type,
        start_ms=start_ms,
        end_ms=end_ms,
    )
