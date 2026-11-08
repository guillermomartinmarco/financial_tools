"""Normalización de señales común a todos los motores (RF-16, plan §3.7, A24).

Se aplica una sola vez, en el núcleo, a la salida de ``Strategy.signals``: los motores
reciben siempre ``entries``/``exits`` ``Boolean``, sin nulos, sin señal durante el
calentamiento y sin entradas y salidas simultáneas.
"""

from __future__ import annotations

import polars as pl

from finances_core.backtest.errors import InvalidSignalsError
from finances_core.backtest.strategies.base import Signals


def finalize(pos: pl.Series, *, n: int, warmup: int) -> pl.Series:
    """Valida y normaliza la posición-objetivo de una estrategia.

    Args:
        pos: salida de ``Strategy.signals``.
        n: número de barras (``candles.height``).
        warmup: ``Strategy.warmup(params)``; la posición es 0 para ``t < warmup``.

    Raises:
        InvalidSignalsError: SI ``pos`` no es casteable a ``Int8`` sin pérdida,
            su longitud difiere de ``n`` o hay nulos en ``t >= warmup``.
    """
    if not isinstance(pos, pl.Series):
        msg = f"invalid_signals: pos debe ser pl.Series (recibido {type(pos).__name__})"
        raise InvalidSignalsError(msg)

    try:
        pos = pos.cast(pl.Int8)
    except pl.ComputeError:
        msg = f"invalid_signals: pos no es casteable a Int8 sin pérdida"
        raise InvalidSignalsError(msg)

    if pos.len() != n:
        msg = f"invalid_signals: pos tiene longitud {pos.len()}, se esperaba {n}"
        raise InvalidSignalsError(msg)

    # Validar dominio {-1, 0, 1} y nulos en t >= warmup
    frame = pl.DataFrame({"pos": pos}).with_columns(
        warm=pl.int_range(pl.len(), dtype=pl.Int64) < max(warmup, 0)
    )
    
    # Nulos en t >= warmup son error
    nulls = frame.filter(~pl.col("warm")).select(pl.col("pos").null_count())
    if nulls.item() > 0:
        msg = f"invalid_signals: hay {nulls.item()} nulos en barras t >= warmup ({warmup})"
        raise InvalidSignalsError(msg)

    # Dominio: cualquier valor fuera de {-1, 0, 1} es error
    out_of_domain = frame.filter(
        ~pl.col("warm") & (~pl.col("pos").is_in([-1, 0, 1]))
    )
    if out_of_domain.height > 0:
        msg = f"invalid_signals: pos contiene valores fuera de domain {{-1, 0, 1}}"
        raise InvalidSignalsError(msg)

    # Normalización: t < warmup -> 0 (plano)
    return frame.select(
        pl.when(pl.col("warm")).then(0).otherwise(pl.col("pos")).cast(pl.Int8).alias("pos")
    )["pos"]


def position_to_signals(pos: pl.Series) -> Signals:
    """Deriva flancos (entries/exits) para el motor largo/plano a partir de la posición.

    Reglas:
    - entries[t] = (pos[t]==1) & (pos[t-1]!=1)
    - exits[t] = (pos[t]!=1) & (pos[t-1]==1)
    - pos[-1] = 0 (la primera barra puede abrir)

    Raises:
        StrategyRuntimeError: SI ``pos`` contiene -1 (corto no soportado hasta G2b).
    """
    if bool((pos == -1).any()):
        from finances_core.backtest.errors import StrategyRuntimeError

        raise StrategyRuntimeError("short no soportado hasta G2b")

    # Shift para obtener pos[t-1], llenando el primer elemento con 0
    prev_pos = pos.shift(1).fill_null(0)
    
    entries = (pos == 1) & (prev_pos != 1)
    exits = (pos != 1) & (prev_pos == 1)
    
    return Signals(entries=entries, exits=exits)
