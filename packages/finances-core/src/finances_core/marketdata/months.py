"""Helpers de meses calendario UTC (particiones ``year``/``month`` y archivos mensuales)."""

from __future__ import annotations

from datetime import UTC, datetime

DECEMBER = 12


def ms_to_year_month(ms: int) -> tuple[int, int]:
    """Convierte timestamp ms UTC a ``(año, mes)``."""
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.year, dt.month


def month_bounds(year: int, month: int) -> tuple[int, int]:
    """``(inicio_mes_ms, inicio_mes_siguiente_ms)`` en UTC."""
    start = datetime(year, month, 1, tzinfo=UTC)
    nxt = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == DECEMBER
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
    return int(start.timestamp() * 1000), int(nxt.timestamp() * 1000)


def get_months_in_range(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Lista de ``(año, mes)`` tocados por ``[start_ms, end_ms]``."""
    year, month = ms_to_year_month(start_ms)
    end_year, end_month = ms_to_year_month(end_ms)
    months: list[tuple[int, int]] = []
    while (year, month) <= (end_year, end_month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == DECEMBER else (year, month + 1)
    return months
