"""Timeframes canónicos ↔ intervalos de Yahoo y límites de historial (RF-2, RF-3, ADR-Y05).

Los límites de Yahoo viven **sólo** en esta tabla de constantes, medidos el
2026-10-25 con yfinance 1.7.0. Un timeframe ausente de ``LOOKBACK_DAYS`` /
``MAX_WINDOW_DAYS`` no tiene límite de historial y se pide en un único pedido.
"""

from __future__ import annotations

from finances_core.schemas.market_data import TF_MS_MAP

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

#: Timeframe canónico (disco y API) -> intervalo de Yahoo (RF-2).
YAHOO_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1d": "1d",
    "1w": "1wk",
    "1M": "1mo",
}

#: Historial disponible desde ``now`` (días). Ausente = sin límite (RF-3).
LOOKBACK_DAYS: dict[str, int] = {"1m": 30, "5m": 60, "15m": 60, "30m": 60, "1h": 730}

#: Ventana máxima por pedido (días). Ausente = un solo pedido (RF-3).
MAX_WINDOW_DAYS: dict[str, int] = {"1m": 8, "5m": 60, "15m": 60, "30m": 60, "1h": 730}

#: Holgura aplicada al borde del historial para no rozar el límite de Yahoo (plan §3.1).
LOOKBACK_MARGIN_MS = HOUR_MS

#: Timeframes cuyas acciones corporativas Yahoo fecha en el día real del evento.
#: En ``1wk``/``1mo`` yfinance las asigna a la fila de la semana/mes que las contiene,
#: por lo que no se usan para la tabla de eventos (evita fechas incorrectas/duplicadas).
EVENT_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "1h", "1d"})

#: Duración mínima/máxima real de una vela de calendario (DST de 23/25 h, meses de 28-31 días).
_MIN_CANDLE_MS: dict[str, int] = {"1d": 23 * HOUR_MS, "1w": 7 * DAY_MS - HOUR_MS, "1M": 28 * DAY_MS}
_MAX_CANDLE_MS: dict[str, int] = {"1d": 25 * HOUR_MS, "1w": 7 * DAY_MS + HOUR_MS, "1M": 31 * DAY_MS}

Window = tuple[int, int]


def min_candle_ms(timeframe: str) -> int:
    """Duración mínima de una vela: cobertura del inventario (última vela + esto).

    Usar el mínimo garantiza que la siguiente vela real (tras un cambio de hora o un
    mes corto) quede siempre dentro del gap y nunca se dé por cubierta sin tenerla.
    """
    return _MIN_CANDLE_MS.get(timeframe, TF_MS_MAP[timeframe])


def max_candle_ms(timeframe: str) -> int:
    """Duración máxima de una vela: si ``open + esto > now`` la vela aún se está formando."""
    return _MAX_CANDLE_MS.get(timeframe, TF_MS_MAP[timeframe])


def _chunks(start_ms: int, end_ms: int, max_window_ms: int | None) -> list[Window]:
    if max_window_ms is None:
        return [(start_ms, end_ms)]
    windows: list[Window] = []
    current = start_ms
    while current < end_ms:
        upper = min(current + max_window_ms, end_ms)
        windows.append((current, upper))
        current = upper
    return windows


def split_by_limits(gap: Window, timeframe: str, now_ms: int) -> tuple[list[Window], list[Window]]:
    """Aplica RF-3 a un gap ``[start, end)``.

    Returns:
        ``(ventanas_descargables, tramos_fuera_de_historial)``. Las ventanas son
        contiguas y no superan ``MAX_WINDOW_DAYS``; lo anterior a
        ``now - LOOKBACK_DAYS + LOOKBACK_MARGIN_MS`` es irrecuperable.
    """
    start_ms, end_ms = gap
    if end_ms <= start_ms:
        return [], []

    unfillable: list[Window] = []
    lookback = LOOKBACK_DAYS.get(timeframe)
    if lookback is not None:
        available_from = now_ms - lookback * DAY_MS + LOOKBACK_MARGIN_MS
        if start_ms < available_from:
            unfillable.append((start_ms, min(end_ms, available_from)))
            start_ms = available_from
    if end_ms <= start_ms:
        return [], unfillable

    max_days = MAX_WINDOW_DAYS.get(timeframe)
    max_window_ms = max_days * DAY_MS if max_days is not None else None
    return _chunks(start_ms, end_ms, max_window_ms), unfillable
