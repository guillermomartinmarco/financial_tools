from typing import List, Optional, Dict, Any, Literal
import polars as pl
import structlog
from datetime import datetime, timedelta

from finances_core.schemas.data_viewer import (
    DatasetIdentifier, DatasetSummary, ColumnStats, GapInfo, AnomalyReport
)
from finances_core.schemas.market_data import TF_MS_MAP, OHLCV_SCHEMA
from finances_core.config import settings

logger = structlog.get_logger(__name__)


CRITICAL_COLS = ["open", "high", "low", "close", "volume"]
PRICE_COLS = ["open", "high", "low", "close"]


def compute_summary(lf: pl.LazyFrame, identifier: DatasetIdentifier) -> DatasetSummary:
    """
    Computa resumen estadístico usando metadata y aggregation lazy.
    """
    # Row count y date range
    agg = lf.select(
        pl.len().alias("row_count"),
        pl.col("open_time_ms").min().alias("min_ts"),
        pl.col("open_time_ms").max().alias("max_ts"),
    ).collect()

    row_count = agg["row_count"][0]
    min_ts = agg["min_ts"][0] if agg["min_ts"][0] else 0
    max_ts = agg["max_ts"][0] if agg["max_ts"][0] else 0

    # Column stats usando metadata + sample para mean/std
    cols = []
    for col_name, dtype in OHLCV_SCHEMA.items():
        if dtype in (pl.Float32, pl.Float64, pl.Int64, pl.Int32):
            # Para numéricas: min, max, mean, std, null_count
            stats = lf.select(
                pl.col(col_name).min().alias("min"),
                pl.col(col_name).max().alias("max"),
                pl.col(col_name).mean().alias("mean"),
                pl.col(col_name).std().alias("std"),
                pl.col(col_name).null_count().alias("null_count"),
                pl.col(col_name).n_unique().alias("distinct_count"),
            ).collect()
            cols.append(ColumnStats(
                name=col_name,
                dtype=str(dtype),
                null_count=stats["null_count"][0] if stats["null_count"][0] else 0,
                distinct_count=stats["distinct_count"][0] if stats["distinct_count"][0] else None,
                min=stats["min"][0] if stats["min"][0] is not None else None,
                max=stats["max"][0] if stats["max"][0] is not None else None,
                mean=stats["mean"][0] if stats["mean"][0] is not None else None,
                std=stats["std"][0] if stats["std"][0] is not None else None,
            ))
        else:
            # Para strings/otros: null_count, distinct_count
            stats = lf.select(
                pl.col(col_name).null_count().alias("null_count"),
                pl.col(col_name).n_unique().alias("distinct_count"),
            ).collect()
            cols.append(ColumnStats(
                name=col_name,
                dtype=str(dtype),
                null_count=stats["null_count"][0] if stats["null_count"][0] else 0,
                distinct_count=stats["distinct_count"][0] if stats["distinct_count"][0] else None,
            ))

    # Gaps detected (aprox usando diff > 1.5 * expected)
    timeframe_ms = TF_MS_MAP.get(identifier.timeframe, 60_000)
    gaps_detected = detect_gaps_count(lf, timeframe_ms)

    # Duplicate timestamps
    dup_count = detect_duplicate_count(lf)

    # Outliers (precios > 3 std)
    outlier_count = detect_outlier_count(lf)

    return DatasetSummary(
        dataset_id=identifier,
        row_count=row_count,
        date_range=(min_ts, max_ts),
        columns=cols,
        gaps_detected=gaps_detected,
        duplicate_timestamps=dup_count,
        outlier_count=outlier_count,
    )


def detect_gaps_count(lf: pl.LazyFrame, timeframe_ms: int) -> int:
    """Cuenta gaps usando diff > 1.5 * timeframe_ms."""
    try:
        gap_df = lf.select(
            pl.col("open_time_ms").diff().alias("diff")
        ).filter(
            pl.col("diff") > timeframe_ms * 1.5
        ).collect()
        return gap_df.height
    except Exception:
        return 0


def detect_duplicate_count(lf: pl.LazyFrame) -> int:
    """Cuenta timestamps duplicados."""
    try:
        dup_df = lf.select(
            pl.col("open_time_ms")
        ).group_by("open_time_ms").agg(
            pl.len().alias("count")
        ).filter(
            pl.col("count") > 1
        ).collect()
        return dup_df.height
    except Exception:
        return 0


def detect_outlier_count(lf: pl.LazyFrame) -> int:
    """Cuenta outliers de precio (Z-score > 3)."""
    try:
        total = 0
        for col in PRICE_COLS:
            stats = lf.select(
                pl.col(col).mean().alias("mean"),
                pl.col(col).std().alias("std"),
            ).collect()
            mean = stats["mean"][0] if stats["mean"][0] else 0
            std = stats["std"][0] if stats["std"][0] else 1
            if std == 0:
                continue
            outliers = lf.filter(
                (pl.col(col) - mean).abs() > 3 * std
            ).collect()
            total += outliers.height
        return total
    except Exception:
        return 0


def detect_gaps(
    lf: pl.LazyFrame,
    timeframe_ms: int,
    min_gap_candles: int = 1,
) -> List[GapInfo]:
    """
    Detecta gaps temporales y retorna lista detallada.
    """
    gaps = []
    try:
        # Obtener timestamps ordenados
        ts_df = lf.select("open_time_ms").sort("open_time_ms").collect()
        if ts_df.height < 2:
            return gaps

        timestamps = ts_df["open_time_ms"].to_list()
        expected_diff = timeframe_ms

        for i in range(1, len(timestamps)):
            prev_ts = timestamps[i - 1]
            curr_ts = timestamps[i]
            actual_diff = curr_ts - prev_ts

            if actual_diff > expected_diff * 1.5:
                missing = int(actual_diff // expected_diff) - 1
                if missing >= min_gap_candles:
                    gap_start = prev_ts + expected_diff
                    gap_end = curr_ts - expected_diff
                    gaps.append(GapInfo(
                        gap_start_ms=gap_start,
                        gap_end_ms=gap_end,
                        missing_candles=missing,
                        gap_duration_human=format_duration_human(actual_diff - expected_diff),
                    ))
    except Exception as e:
        logger.warning("gap_detection_failed", error=str(e))

    return gaps


def detect_anomalies(lf: pl.LazyFrame, identifier: Optional[DatasetIdentifier] = None) -> AnomalyReport:
    """
    Detecta anomalías: duplicados, nulos, outliers precio, volumen anómalo.
    """
    # Duplicate timestamps
    dup_examples = []
    try:
        dup_df = lf.select("open_time_ms").group_by("open_time_ms").agg(
            pl.len().alias("count")
        ).filter(pl.col("count") > 1).limit(10).collect()
        dup_examples = [{"timestamp": row["open_time_ms"], "count": row["count"]} for row in dup_df.iter_rows(named=True)]
    except Exception:
        pass

    duplicate_timestamps = {"count": len(dup_examples), "examples": dup_examples}

    # Null values en columnas críticas
    null_values = {}
    for col in CRITICAL_COLS:
        try:
            null_count = lf.select(pl.col(col).null_count()).collect()["null_count"][0]
            if null_count > 0:
                null_values[col] = null_count
        except Exception:
            pass

    # Price outliers (Z-score > 3)
    price_outliers = {}
    for col in PRICE_COLS:
        try:
            stats = lf.select(
                pl.col(col).mean().alias("mean"),
                pl.col(col).std().alias("std"),
            ).collect()
            mean = stats["mean"][0] if stats["mean"][0] else 0
            std = stats["std"][0] if stats["std"][0] else 1
            if std == 0:
                continue
            outliers_df = lf.filter(
                (pl.col(col) - mean).abs() > 3 * std
            ).select("open_time_ms", col).limit(20).collect()
            if outliers_df.height > 0:
                price_outliers[col] = [
                    {"timestamp": row["open_time_ms"], "value": row[col], "zscore": abs((row[col] - mean) / std)}
                    for row in outliers_df.iter_rows(named=True)
                ]
        except Exception:
            pass

    # Volume anomalies (volumen == 0 o > 3 std)
    volume_anomalies = {}
    try:
        col = "volume"
        # Volumen = 0
        zero_vol = lf.filter(pl.col(col) == 0).select("open_time_ms", col).limit(20).collect()
        # Volumen > 3 std
        stats = lf.select(pl.col(col).mean().alias("mean"), pl.col(col).std().alias("std")).collect()
        mean = stats["mean"][0] if stats["mean"][0] else 0
        std = stats["std"][0] if stats["std"][0] else 1
        high_vol = lf.filter(pl.col(col) > mean + 3 * std).select("open_time_ms", col).limit(20).collect()

        anomalies = []
        for row in zero_vol.iter_rows(named=True):
            anomalies.append({"timestamp": row["open_time_ms"], "value": row[col], "type": "zero_volume"})
        for row in high_vol.iter_rows(named=True):
            zscore = (row[col] - mean) / std if std > 0 else 0
            anomalies.append({"timestamp": row["open_time_ms"], "value": row[col], "type": "high_volume", "zscore": zscore})

        if anomalies:
            volume_anomalies[col] = anomalies
    except Exception:
        pass

    # Usar identificador por defecto si no se proporciona
    if identifier is None:
        identifier = DatasetIdentifier(exchange="unknown", market_type="spot", symbol="unknown", timeframe="unknown")
    
    return AnomalyReport(
        dataset_id=identifier,
        duplicate_timestamps=duplicate_timestamps,
        null_values=null_values,
        price_outliers=price_outliers,
        volume_anomalies=volume_anomalies,
    )


def sample_rows(
    lf: pl.LazyFrame,
    n: int,
    mode: Literal["head", "tail", "sample"],
    cols: Optional[List[str]] = None,
) -> pl.DataFrame:
    """
    Retorna muestra de filas: head, tail, o reservoir sampling.
    """
    if cols:
        lf = lf.select(cols)

    if mode == "head":
        return lf.sort("open_time_ms").limit(n).collect()
    elif mode == "tail":
        return lf.sort("open_time_ms", descending=True).limit(n).collect()
    else:  # sample - reservoir sampling
        # Collect first, then sample from DataFrame
        df = lf.collect()
        total = df.height
        if total <= n:
            return df
        return df.sample(n=n, with_replacement=False, seed=42)


def format_duration_human(ms: int) -> str:
    """Formatea milisegundos a string legible."""
    td = timedelta(milliseconds=ms)
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)