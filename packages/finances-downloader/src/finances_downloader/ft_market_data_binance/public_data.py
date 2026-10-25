"""RF-1 paso 4: qué meses de cada gap existen en Binance Public Data.

Cada mes se evalúa por separado: los meses con .zip publicado se convierten en
items ``public_data`` y **cada** mes ausente (p. ej. el mes en curso, que Binance
aún no publica) se devuelve como sub-rango residual para el fallback REST (paso 5).
"""

from __future__ import annotations

import asyncio

import structlog
from finances_core.marketdata.months import (
    DECEMBER,
    get_months_in_range,
    month_bounds,
    ms_to_year_month,
)
from finances_core.schemas.market_data import DownloadPlanItem

from finances_downloader.ft_market_data_binance.client import BinancePublicDataClient

logger = structlog.get_logger(__name__)

__all__ = [
    "DECEMBER",
    "check_public_data_availability",
    "get_months_in_range",
    "month_bounds",
    "ms_to_year_month",
]


def _merge_adjacent(segments: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in segments:
        if merged and merged[-1][1] >= start:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


async def check_public_data_availability(
    gaps: list[tuple[int, int]],
    symbol: str,
    timeframe: str,
    market_type: str,
    client: BinancePublicDataClient,
) -> tuple[list[DownloadPlanItem], list[tuple[int, int]]]:
    """Para cada gap y cada mes: HEAD del .zip mensual.

    Returns:
        ``(items_public_data, sub_rangos_no_cubiertos)``; los sub-rangos contiguos
        se fusionan para minimizar requests REST.
    """
    public_items: list[DownloadPlanItem] = []
    uncovered: list[tuple[int, int]] = []

    for gap_start, gap_end in gaps:
        segments: list[tuple[int, int, int, int]] = []
        for year, month in get_months_in_range(gap_start, gap_end):
            month_start, next_month = month_bounds(year, month)
            seg_start, seg_end = max(gap_start, month_start), min(gap_end, next_month)
            if seg_start < seg_end:
                segments.append((year, month, seg_start, seg_end))

        checks = await asyncio.gather(
            *(client.check_exists(symbol, timeframe, market_type, y, m) for y, m, _, _ in segments)
        )
        for (year, month, seg_start, seg_end), (exists, checksum) in zip(
            segments, checks, strict=True
        ):
            if exists:
                public_items.append(
                    DownloadPlanItem(
                        source="public_data",
                        symbol=symbol,
                        timeframe=timeframe,
                        market_type=market_type,
                        url=client.build_monthly_url(symbol, timeframe, market_type, year, month),
                        expected_checksum=checksum,
                        covers_range_ms=[seg_start, seg_end],
                    )
                )
            else:
                logger.debug(
                    "public_data_month_missing",
                    symbol=symbol,
                    timeframe=timeframe,
                    year=year,
                    month=month,
                )
                uncovered.append((seg_start, seg_end))

    return public_items, _merge_adjacent(uncovered)
