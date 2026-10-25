
import structlog
from finances_core.schemas.market_data import TF_MS_MAP, DownloadPlanItem

from finances_downloader.ft_market_data_binance.client import BinanceRestClient

logger = structlog.get_logger(__name__)


def plan_rest_fallback(
    uncovered_gaps: list[tuple[int, int]],
    symbol: str,
    timeframe: str,
    market_type: str,
    limit: int,
    client: BinanceRestClient,
) -> list[DownloadPlanItem]:
    """
    Para gaps no cubiertos por Public Data, planifica requests a REST API
    con paginación (limit=1000 por request).
    """
    items = []
    interval_ms = TF_MS_MAP.get(timeframe, 60_000)

    for gap_start, gap_end in uncovered_gaps:
        current_start = gap_start
        while current_start < gap_end:
            current_end = min(gap_end, current_start + limit * interval_ms)
            params = {
                "symbol": symbol.upper(),
                "interval": timeframe,
                "startTime": current_start,
                "endTime": current_end,
                "limit": limit,
            }
            endpoint = "/api/v3/klines" if market_type == "spot" else "/fapi/v1/klines"
            base_url = client.base_url_spot if market_type == "spot" else client.base_url_futures

            item = DownloadPlanItem(
                source="rest_api",
                symbol=symbol,
                timeframe=timeframe,
                market_type=market_type,
                endpoint=endpoint,
                params=params,
                covers_range_ms=[current_start, current_end],
            )
            items.append(item)
            current_start = current_end

    return items
