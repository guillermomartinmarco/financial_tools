from typing import Literal

import polars as pl
import structlog
from finances_core.schemas.market_data import OHLCV_SCHEMA

logger = structlog.get_logger(__name__)

PUBLIC_DATA_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "number_of_trades",
    "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
]

REST_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "number_of_trades",
    "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
]


def normalize_binance_klines(
    df: pl.DataFrame,
    source: Literal["public_data", "rest_api"],
    symbol: str,
    timeframe: str,
    market_type: str,
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    """
    Normaliza klines crudos (CSV Public Data o JSON REST) al esquema canónico.
    """
    if source == "public_data":
        # CSV ya tiene headers
        df = df.rename({old: new for old, new in zip(PUBLIC_DATA_COLS, [
            "open_time_ms", "open", "high", "low", "close", "volume",
            "close_time_ms", "quote_volume", "trades_count",
            "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"
        ])})
    else:
        # REST JSON: DataFrame ya tiene columnas de REST_COLS
        df = df.rename({old: new for old, new in zip(REST_COLS, [
            "open_time_ms", "open", "high", "low", "close", "volume",
            "close_time_ms", "quote_volume", "trades_count",
            "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"
        ])})

    # Cast a tipos canónicos
    df = df.with_columns([
        pl.col("open_time_ms").cast(pl.Int64).alias("open_time_ms"),
        pl.col("open").cast(pl.Float32),
        pl.col("high").cast(pl.Float32),
        pl.col("low").cast(pl.Float32),
        pl.col("close").cast(pl.Float32),
        pl.col("volume").cast(pl.Float32),
        pl.col("quote_volume").cast(pl.Float32),
        pl.col("trades_count").cast(pl.Int64),
        pl.col("taker_buy_base_volume").cast(pl.Float32),
        pl.col("taker_buy_quote_volume").cast(pl.Float32),
    ])

    # Filtrar rango solicitado (por si CSV trae velas extra)
    df = df.filter(
        (pl.col("open_time_ms") >= start_ms) &
        (pl.col("open_time_ms") <= end_ms)
    )

    # Deduplicar por open_time_ms (mantener primera)
    df = df.unique(subset=["open_time_ms"], maintain_order=True)

    # Ordenar por timestamp
    df = df.sort("open_time_ms")

    # Agregar columnas derivadas
    df = df.with_columns([
        pl.lit(symbol).cast(pl.Utf8).alias("symbol"),
        pl.lit(timeframe).cast(pl.Utf8).alias("timeframe"),
        pl.lit(market_type).cast(pl.Utf8).alias("market_type"),
        (pl.col("open_time_ms") // 86_400_000 * 86_400_000).cast(pl.Int64).alias("day_start_ms"),
    ])

    # Agregar year, month para particionado
    df = df.with_columns([
        (pl.col("open_time_ms") // 1000).cast(pl.Int64).alias("ts_sec"),
    ]).with_columns([
        pl.from_epoch("ts_sec", time_unit="s").dt.year().cast(pl.Int32).alias("year"),
        pl.from_epoch("ts_sec", time_unit="s").dt.month().cast(pl.Int32).alias("month"),
    ]).drop(["ts_sec", "day_start_ms", "ignore", "close_time_ms"])

    # Validar schema final - OHLCV_SCHEMA son las columnas canónicas de datos;
    # year/month son columnas de particionado requeridas para escritura.
    expected_cols = set(OHLCV_SCHEMA.keys())
    partition_cols = {"year", "month"}
    actual_cols = set(df.columns)
    missing = expected_cols - actual_cols
    extra = actual_cols - expected_cols - partition_cols
    if missing:
        raise ValueError(f"Columnas faltantes tras normalización: {missing}")
    if extra:
        logger.warning("normalizer_extra_columns", extra=list(extra))
    # Mantener columnas canónicas + partición
    df = df.select(list(expected_cols) + ["year", "month"])

    # Verificar dtypes (solo columnas canónicas)
    for col, dtype in OHLCV_SCHEMA.items():
        if col in df.columns and df[col].dtype != dtype:
            logger.warning("dtype_mismatch", column=col, expected=dtype, actual=df[col].dtype)

    return df


def read_public_data_csv(zip_path: str, symbol: str, timeframe: str, market_type: str, start_ms: int, end_ms: int) -> pl.DataFrame:
    """Lee CSV desde archivo .zip de Public Data y normaliza."""
    df = pl.read_csv(
        zip_path,
        has_header=True,
        new_columns=PUBLIC_DATA_COLS,
        schema_overrides=dict.fromkeys(PUBLIC_DATA_COLS, pl.Utf8),
    )
    return normalize_binance_klines(df, "public_data", symbol, timeframe, market_type, start_ms, end_ms)


def normalize_rest_json(json_data: list[list[str | int | float]], symbol: str, timeframe: str, market_type: str, start_ms: int, end_ms: int) -> pl.DataFrame:
    """Normaliza respuesta JSON de REST API."""
    df = pl.DataFrame(json_data, schema=REST_COLS, orient="row")
    return normalize_binance_klines(df, "rest_api", symbol, timeframe, market_type, start_ms, end_ms)
