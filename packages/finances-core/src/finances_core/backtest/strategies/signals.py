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
    except (pl.exceptions.ComputeError, pl.exceptions.InvalidOperationError):
        msg = "invalid_signals: pos no es casteable a Int8 sin pérdida"
        raise InvalidSignalsError(msg) from None

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
    out_of_domain = frame.filter(~pl.col("warm") & (~pl.col("pos").is_in([-1, 0, 1])))
    if out_of_domain.height > 0:
        msg = "invalid_signals: pos contiene valores fuera de domain {-1, 0, 1}"
        raise InvalidSignalsError(msg)

    # Normalización: t < warmup -> 0 (plano)
    return frame.select(
        pl.when(pl.col("warm")).then(0).otherwise(pl.col("pos")).cast(pl.Int8).alias("position")
    )["position"]


def position_to_signals(pos: pl.Series) -> Signals:
    """Deriva los 4 flancos largo/corto para el motor a partir de la posición (Enmienda G2b).

    Reglas (``prev = pos[t-1]``, con ``pos[-1] = 0``: la primera barra puede abrir):
    - ``entries[t] = (pos[t]==1) ∧ (prev≠ 1)``; ``exits[t] = (pos[t]≠1) ∧ (prev==1)``.
    - ``entries_short[t] = (pos[t]==-1) ∧ (prev≠-1)``;
      ``exits_short[t] = (pos[t]≠-1) ∧ (prev==-1)``.

    Raises:
        InvalidSignalsError: SI ``pos`` salta de ``+1`` a ``-1`` (o viceversa) sin pasar por
            ``0`` en una barra intermedia (decisión de producto, Enmienda G2b: una estrategia
            que quiera invertir debe emitir un ``0`` explícito antes del lado opuesto).
    """
    prev_pos = pos.shift(1).fill_null(0)

    direct_reversal = ((prev_pos == 1) & (pos == -1)) | ((prev_pos == -1) & (pos == 1))
    if bool(direct_reversal.any()):
        t = int(direct_reversal.arg_true()[0])
        msg = f"invalid_signals: reversa directa +1<->-1 en t={t} (sin 0 intermedio)"
        raise InvalidSignalsError(msg)

    entries = (pos == 1) & (prev_pos != 1)
    exits = (pos != 1) & (prev_pos == 1)
    entries_short = (pos == -1) & (prev_pos != -1)
    exits_short = (pos != -1) & (prev_pos == -1)

    return Signals(
        entries=entries, exits=exits, entries_short=entries_short, exits_short=exits_short
    )
