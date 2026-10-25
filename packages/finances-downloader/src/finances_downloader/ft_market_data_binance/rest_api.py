"""RF-1 paso 5: planificación del fallback REST paginado."""

from __future__ import annotations

from finances_core.schemas.market_data import TF_MS_MAP, DownloadPlanItem

from finances_downloader.ft_market_data_binance.client import BinanceRestClient


def plan_rest_fallback(
    uncovered_gaps: list[tuple[int, int]],
    symbol: str,
    timeframe: str,
    market_type: str,
    limit: int,
    client: BinanceRestClient,
) -> list[DownloadPlanItem]:
    """Trocea cada gap no cubierto en items REST de ``limit`` velas como máximo."""
    items: list[DownloadPlanItem] = []
    interval_ms = TF_MS_MAP[timeframe]
    endpoint = client.endpoint(market_type)

    for gap_start, gap_end in uncovered_gaps:
        current_start = gap_start
        while current_start < gap_end:
            current_end = min(gap_end, current_start + limit * interval_ms)
            items.append(
                DownloadPlanItem(
                    source="rest_api",
                    symbol=symbol,
                    timeframe=timeframe,
                    market_type=market_type,
                    endpoint=endpoint,
                    params={
                        "symbol": symbol.upper(),
                        "interval": timeframe,
                        "startTime": current_start,
                        "endTime": current_end,
                        "limit": limit,
                    },
                    covers_range_ms=[current_start, current_end],
                )
            )
            current_start = current_end

    return items
