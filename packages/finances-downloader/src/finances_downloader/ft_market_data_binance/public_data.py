from datetime import UTC, datetime

import structlog
from finances_core.schemas.market_data import DownloadPlanItem

from finances_downloader.ft_market_data_binance.client import BinancePublicDataClient

logger = structlog.get_logger(__name__)


def ms_to_year_month(ms: int) -> tuple[int, int]:
    """Convierte timestamp ms a (año, mes)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.year, dt.month


def get_months_in_range(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Retorna lista de (año, mes) cubiertos por el rango [start_ms, end_ms]."""
    start_year, start_month = ms_to_year_month(start_ms)
    end_year, end_month = ms_to_year_month(end_ms)

    months = []
    year, month = start_year, start_month
    while (year < end_year) or (year == end_year and month <= end_month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


async def check_public_data_availability(
    gaps: list[tuple[int, int]],
    symbol: str,
    timeframe: str,
    market_type: str,
    client: BinancePublicDataClient,
) -> tuple[list[DownloadPlanItem], list[tuple[int, int]]]:
    """
    Para cada gap, verifica qué archivos mensuales existen en Public Data.
    Retorna (items_public_data, gaps_residuales_no_cubiertos).
    """
    public_items = []
    uncovered_gaps = []

    for gap_start, gap_end in gaps:
        months = get_months_in_range(gap_start, gap_end)
        gap_covered = False

        for year, month in months:
            exists, checksum = await client.check_exists(symbol, timeframe, market_type, year, month)
            if exists:
                gap_covered = True
                # Calcular rango real cubierto por este archivo mensual
                month_start = int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)
                if month == 12:
                    next_month_start = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp() * 1000)
                else:
                    next_month_start = int(datetime(year, month + 1, 1, tzinfo=UTC).timestamp() * 1000)

                covers_start = max(gap_start, month_start)
                covers_end = min(gap_end, next_month_start)

                url = client._build_monthly_url(symbol, timeframe, market_type, year, month)
                item = DownloadPlanItem(
                    source="public_data",
                    symbol=symbol,
                    timeframe=timeframe,
                    market_type=market_type,
                    url=url,
                    expected_checksum=checksum,
                    covers_range_ms=[covers_start, covers_end],
                )
                public_items.append(item)

        if not gap_covered:
            uncovered_gaps.append((gap_start, gap_end))

    return public_items, uncovered_gaps
