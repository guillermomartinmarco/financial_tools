from typing import Optional, List
import polars as pl
import structlog

from finances_core.schemas.data_viewer import (
    DatasetIdentifier, CompareResult, GapInfo
)

logger = structlog.get_logger(__name__)


def compare_datasets(
    lf_a: pl.LazyFrame,
    lf_b: pl.LazyFrame,
    identifier_a: DatasetIdentifier,
    identifier_b: DatasetIdentifier,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> CompareResult:
    """
    Compara dos datasets (mismo symbol/timeframe o distinto exchange/market).
    Retorna métricas de divergencia.
    """
    # Filtrar por rango temporal si se especifica
    if start_ms is not None:
        lf_a = lf_a.filter(pl.col("open_time_ms") >= start_ms)
        lf_b = lf_b.filter(pl.col("open_time_ms") >= start_ms)
    if end_ms is not None:
        lf_a = lf_a.filter(pl.col("open_time_ms") <= end_ms)
        lf_b = lf_b.filter(pl.col("open_time_ms") <= end_ms)

    # Row counts
    count_a = lf_a.select(pl.len()).collect()["len"][0]
    count_b = lf_b.select(pl.len()).collect()["len"][0]
    row_count_diff = count_a - count_b

    # Date range overlap
    range_a = lf_a.select(
        pl.col("open_time_ms").min().alias("min_a"),
        pl.col("open_time_ms").max().alias("max_a"),
    ).collect()
    range_b = lf_b.select(
        pl.col("open_time_ms").min().alias("min_b"),
        pl.col("open_time_ms").max().alias("max_b"),
    ).collect()

    min_a = range_a["min_a"][0] if range_a["min_a"][0] else None
    max_a = range_a["max_a"][0] if range_a["max_a"][0] else None
    min_b = range_b["min_b"][0] if range_b["min_b"][0] else None
    max_b = range_b["max_b"][0] if range_b["max_b"][0] else None

    date_range_overlap = None
    if min_a is not None and min_b is not None and max_a is not None and max_b is not None:
        overlap_start = max(min_a, min_b)
        overlap_end = min(max_a, max_b)
        if overlap_start <= overlap_end:
            date_range_overlap = (overlap_start, overlap_end)

    # Price divergence (max abs diff en close) - join on open_time_ms
    price_divergence_max = 0.0
    try:
        # Solo timestamps en común
        common_ts = lf_a.select("open_time_ms").join(
            lf_b.select("open_time_ms"),
            on="open_time_ms",
            how="inner"
        ).select("open_time_ms")

        if common_ts.collect().height > 0:
            close_a = lf_a.join(common_ts, on="open_time_ms", how="inner").select("open_time_ms", "close")
            close_b = lf_b.join(common_ts, on="open_time_ms", how="inner").select("open_time_ms", "close")

            diff_df = close_a.join(close_b, on="open_time_ms", suffix="_b").select(
                (pl.col("close") - pl.col("close_b")).abs().alias("diff")
            )
            max_diff = diff_df.select(pl.col("diff").max()).collect()["diff"][0]
            if max_diff is not None:
                price_divergence_max = float(max_diff)
    except Exception as e:
        logger.warning("price_divergence_calc_failed", error=str(e))

    # Volume divergence pct (promedio relativo)
    volume_divergence_pct = 0.0
    try:
        vol_a = lf_a.select(pl.col("volume").mean()).collect()["volume"][0]
        vol_b = lf_b.select(pl.col("volume").mean()).collect()["volume"][0]
        if vol_a and vol_a > 0:
            volume_divergence_pct = abs(float(vol_a - vol_b) / vol_a) * 100
    except Exception:
        pass

    # Gaps only in A / only in B (simplificado - comparar gaps detectados)
    # Para MVP retornamos listas vacías; implementación completa requiere detector de gaps
    gaps_only_in_a: List[GapInfo] = []
    gaps_only_in_b: List[GapInfo] = []

    return CompareResult(
        dataset_a=identifier_a,
        dataset_b=identifier_b,
        row_count_diff=row_count_diff,
        date_range_overlap=date_range_overlap,
        price_divergence_max=price_divergence_max,
        volume_divergence_pct=volume_divergence_pct,
        gaps_only_in_a=gaps_only_in_a,
        gaps_only_in_b=gaps_only_in_b,
    )