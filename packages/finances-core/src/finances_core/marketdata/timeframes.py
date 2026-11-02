"""Grilla de timeframes de velas: inicio válido de cada vela en ms UTC (``ft_market_data_manager``).

Reglas (RF-5): los timeframes de duración fija caen en múltiplos exactos de su duración desde
la época Unix; ``1w`` empieza en lunes 00:00 UTC y ``1M`` en el día 1 a las 00:00 UTC. La
duración fija reutiliza ``TF_MS_MAP`` (``schemas.market_data``); ``1M`` no tiene duración fija
(la entrada de ``TF_MS_MAP`` es una aproximación de 30 días que aquí no se usa).
"""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from finances_core.schemas.market_data import TF_MS_MAP

DAY_MS = 86_400_000
WEEK_TIMEFRAME = "1w"
MONTH_TIMEFRAME = "1M"

#: La época Unix (jueves 1970-01-01) está 3 días después de un lunes.
_EPOCH_DAYS_TO_MONDAY = 3
_DAYS_PER_WEEK = 7


def _check_known(timeframe: str) -> None:
    if timeframe not in TF_MS_MAP:
        msg = f"timeframe desconocido: {timeframe!r}"
        raise ValueError(msg)


def timeframe_duration_ms(timeframe: str) -> int:
    """Duración en ms de un timeframe (``1w`` = 7 días; ``1M`` es aproximada: 30 días).

    Raises:
        ValueError: SI el timeframe no es uno de los soportados por la suite.
    """
    _check_known(timeframe)
    return TF_MS_MAP[timeframe]


def is_aligned(ts_ms: int, timeframe: str) -> bool:
    """SI ``ts_ms`` es el inicio exacto de una vela del timeframe (lunes para ``1w``, día 1
    para ``1M``).

    Raises:
        ValueError: SI el timeframe es desconocido.
    """
    _check_known(timeframe)
    if timeframe == MONTH_TIMEFRAME:
        if ts_ms % DAY_MS:
            return False
        return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).day == 1
    if timeframe == WEEK_TIMEFRAME:
        days, rest = divmod(ts_ms, DAY_MS)
        return rest == 0 and (days + _EPOCH_DAYS_TO_MONDAY) % _DAYS_PER_WEEK == 0
    return ts_ms % TF_MS_MAP[timeframe] == 0


def aligned_expr(column: pl.Expr, timeframe: str) -> pl.Expr:
    """Expresión booleana equivalente a ``is_aligned`` para una columna ``Int64`` de ms UTC.

    Raises:
        ValueError: SI el timeframe es desconocido.
    """
    _check_known(timeframe)
    on_midnight = column % DAY_MS == 0
    if timeframe == MONTH_TIMEFRAME:
        return on_midnight & (pl.from_epoch(column, time_unit="ms").dt.day() == 1)
    if timeframe == WEEK_TIMEFRAME:
        monday = ((column // DAY_MS) + _EPOCH_DAYS_TO_MONDAY) % _DAYS_PER_WEEK == 0
        return on_midnight & monday
    return column % TF_MS_MAP[timeframe] == 0
