"""Normalización de señales común a todos los motores (RF-16, plan §3.7, A24).

Se aplica una sola vez, en el núcleo, a la salida de ``Strategy.signals``: los motores
reciben siempre ``entries``/``exits`` ``Boolean``, sin nulos, sin señal durante el
calentamiento y sin entradas y salidas simultáneas.
"""

from __future__ import annotations

import polars as pl

from finances_core.backtest.errors import InvalidSignalsError
from finances_core.backtest.strategies.base import Signals


def _check_series(name: str, value: object, n: int) -> pl.Series:
    if not isinstance(value, pl.Series):
        msg = f"invalid_signals: {name} debe ser pl.Series (recibido {type(value).__name__})"
        raise InvalidSignalsError(msg)
    if value.dtype != pl.Boolean:
        msg = f"invalid_signals: {name} debe ser Boolean (recibido {value.dtype})"
        raise InvalidSignalsError(msg)
    if value.len() != n:
        msg = f"invalid_signals: {name} tiene longitud {value.len()}, se esperaba {n}"
        raise InvalidSignalsError(msg)
    return value


def finalize(sig: Signals, *, n: int, warmup: int) -> Signals:
    """Valida y normaliza las señales de una estrategia.

    Args:
        sig: salida de ``Strategy.signals``.
        n: número de barras (``candles.height``).
        warmup: ``Strategy.warmup(params)``; las barras ``t < warmup`` no tienen señal.

    Raises:
        InvalidSignalsError: SI ``entries``/``exits`` no son ``pl.Series`` ``Boolean``,
            su longitud difiere de ``n`` o hay nulos en ``t >= warmup``.
    """
    entries = _check_series("entries", getattr(sig, "entries", None), n)
    exits = _check_series("exits", getattr(sig, "exits", None), n)
    frame = pl.DataFrame({"entries": entries, "exits": exits}).with_columns(
        warm=pl.int_range(pl.len(), dtype=pl.Int64) < max(warmup, 0)
    )
    nulls = frame.filter(~pl.col("warm")).select(
        pl.col("entries").null_count() + pl.col("exits").null_count()
    )
    if nulls.item() > 0:
        msg = f"invalid_signals: hay {nulls.item()} nulos en barras t >= warmup ({warmup})"
        raise InvalidSignalsError(msg)
    cleaned = frame.select(
        pl.when(pl.col("warm")).then(False).otherwise(pl.col(name).fill_null(False)).alias(name)
        for name in ("entries", "exits")
    )
    both = pl.col("entries") & pl.col("exits")
    out = cleaned.select(
        (pl.col("entries") & ~both).alias("entries"),
        (pl.col("exits") & ~both).alias("exits"),
    )
    return Signals(entries=out["entries"], exits=out["exits"])
