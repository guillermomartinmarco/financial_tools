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


def position_from_long_short(
    enter_long: pl.Series, exit_long: pl.Series, enter_short: pl.Series, exit_short: pl.Series
) -> pl.Series:
    """Máquina de estados de 3 estados (largo/plano/corto) a partir de 4 eventos
    independientes (Enmienda G3). A diferencia de ``_lib`` (``pl.Expr -> pl.Expr``,
    componible), recibe y devuelve ``pl.Series`` **materializadas**: el estado de la
    barra ``t`` depende del estado de ``t-1``, no es expresable sin estado.

    Reglas (estado inicial ``0``, plano; nulos de entrada se tratan como ``False``):

    - **Largo** (``s=1``): SI ``enter_short[t]`` ENTONCES pasa a plano esta barra y
      **recuerda** entrar corto en ``t+1`` (incondicional, sin depender de que
      ``enter_short`` siga activo: los eventos de cruce son pulsos de una barra); SI NO
      (``exit_long[t]``) pasa a plano sin reversa pendiente; si no, se sostiene.
    - **Corto** (``s=-1``): simétrico con ``enter_long``/``exit_short``.
    - **Plano** (``s=0``): si hay una reversa pendiente de la barra anterior, se aplica
      incondicionalmente (ignora las señales frescas de esta barra, ya decidida); si no,
      ``enter_long`` abre largo, si no ``enter_short`` abre corto (prioridad a largo en
      empate).

    ``exit_long``/``exit_short`` solo se eval\u00faan mientras se est\u00e1 en SU lado exacto:
    una condici\u00f3n de salida pensada para un lado nunca cierra el otro por error (el bug
    que motiva esta m\u00e1quina de estados en vez de una f\u00f3rmula sin estado). Garantiza
    "sin reversa directa" (Enmienda G2b) **por construcci\u00f3n**: toda reversa pasa por
    ``pending``, nunca aparecen ``+1``/``-1`` en barras consecutivas.
    """
    n = enter_long.len()
    el = enter_long.fill_null(False).to_list()
    xl = exit_long.fill_null(False).to_list()
    es = enter_short.fill_null(False).to_list()
    xs = exit_short.fill_null(False).to_list()
    out: list[int] = [0] * n
    state = 0
    pending: int | None = None
    for t in range(n):
        if pending is not None:
            state, pending = pending, None
        elif state == 1:
            if es[t]:
                state, pending = 0, -1
            elif xl[t]:
                state = 0
        elif state == -1:
            if el[t]:
                state, pending = 0, 1
            elif xs[t]:
                state = 0
        else:
            state = 1 if el[t] else (-1 if es[t] else 0)
        out[t] = state
    return pl.Series(out, dtype=pl.Int8)
