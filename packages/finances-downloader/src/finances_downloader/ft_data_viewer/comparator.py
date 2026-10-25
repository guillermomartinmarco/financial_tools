"""Comparación de dos datasets OHLCV (RF-7)."""

from __future__ import annotations

import polars as pl
import structlog
from finances_core.schemas.data_viewer import CompareResult, DatasetIdentifier, GapInfo
from finances_core.schemas.market_data import TF_MS_MAP

from finances_downloader.ft_data_viewer.analyzer import TS_COL, detect_gaps, format_duration_human

logger = structlog.get_logger(__name__, component="ft_data_viewer")


def subtract_gaps(
    gaps: list[GapInfo],
    other: list[GapInfo],
    timeframe_ms: int,
    window: tuple[int, int],
) -> list[GapInfo]:
    """Partes de ``gaps`` (recortadas a ``window``) no cubiertas por ningún gap de ``other``.

    Los intervalos son inclusivos y alineados a la rejilla de ``timeframe_ms``.
    """
    lo, hi = window
    blockers = sorted((g.gap_start_ms, g.gap_end_ms) for g in other)
    result: list[GapInfo] = []
    for gap in gaps:
        start, end = max(gap.gap_start_ms, lo), min(gap.gap_end_ms, hi)
        pieces: list[tuple[int, int]] = []
        cursor = start
        for b_start, b_end in blockers:
            if b_end < cursor or b_start > end:
                continue
            if b_start > cursor:
                pieces.append((cursor, b_start - timeframe_ms))
            cursor = max(cursor, b_end + timeframe_ms)
            if cursor > end:
                break
        if cursor <= end:
            pieces.append((cursor, end))
        for p_start, p_end in pieces:
            if p_end < p_start:
                continue
            missing = (p_end - p_start) // timeframe_ms + 1
            result.append(
                GapInfo(
                    gap_start_ms=p_start,
                    gap_end_ms=p_end,
                    missing_candles=missing,
                    gap_duration_human=format_duration_human(missing * timeframe_ms),
                )
            )
    return result


def _range(lf: pl.LazyFrame) -> tuple[int, int | None, int | None]:
    row = (
        lf.select(
            pl.len().alias("n"), pl.col(TS_COL).min().alias("lo"), pl.col(TS_COL).max().alias("hi")
        )
        .collect()
        .row(0, named=True)
    )
    lo, hi = row["lo"], row["hi"]
    return int(row["n"]), None if lo is None else int(lo), None if hi is None else int(hi)


def compare_datasets(
    lf_a: pl.LazyFrame,
    lf_b: pl.LazyFrame,
    identifier_a: DatasetIdentifier,
    identifier_b: DatasetIdentifier,
) -> CompareResult:
    """Compara A vs B: diferencia de filas, solape temporal, divergencia de ``close``/``volume``
    y gaps exclusivos de cada lado dentro del rango solapado.

    El recorte por rango temporal (``start_ms``/``end_ms``) lo aplica el servicio antes.
    """
    count_a, min_a, max_a = _range(lf_a)
    count_b, min_b, max_b = _range(lf_b)

    overlap: tuple[int, int] | None = None
    if min_a is not None and max_a is not None and min_b is not None and max_b is not None:
        lo, hi = max(min_a, min_b), min(max_a, max_b)
        if lo <= hi:
            overlap = (lo, hi)

    price_divergence_max = 0.0
    if overlap is not None:
        joined = lf_a.select(TS_COL, "close").join(
            lf_b.select(TS_COL, "close"), on=TS_COL, how="inner", suffix="_b"
        )
        max_diff = joined.select((pl.col("close") - pl.col("close_b")).abs().max()).collect().item()
        if max_diff is not None:
            price_divergence_max = float(max_diff)

    volume_divergence_pct = 0.0
    vol = (
        pl.concat(
            [
                lf_a.select(pl.col("volume").mean().alias("v")),
                lf_b.select(pl.col("volume").mean().alias("v")),
            ]
        )
        .collect()["v"]
        .to_list()
    )
    vol_a, vol_b = vol
    if vol_a and vol_b is not None:
        volume_divergence_pct = abs(float(vol_a) - float(vol_b)) / float(vol_a) * 100

    gaps_only_a: list[GapInfo] = []
    gaps_only_b: list[GapInfo] = []
    if overlap is not None:
        tf_a = TF_MS_MAP[identifier_a.timeframe]
        tf_b = TF_MS_MAP[identifier_b.timeframe]
        gaps_a = detect_gaps(lf_a, tf_a)
        gaps_b = detect_gaps(lf_b, tf_b)
        gaps_only_a = subtract_gaps(gaps_a, gaps_b, tf_a, overlap)
        gaps_only_b = subtract_gaps(gaps_b, gaps_a, tf_b, overlap)

    return CompareResult(
        dataset_a=identifier_a,
        dataset_b=identifier_b,
        row_count_diff=count_a - count_b,
        date_range_overlap=overlap,
        price_divergence_max=price_divergence_max,
        volume_divergence_pct=volume_divergence_pct,
        gaps_only_in_a=gaps_only_a,
        gaps_only_in_b=gaps_only_b,
    )
