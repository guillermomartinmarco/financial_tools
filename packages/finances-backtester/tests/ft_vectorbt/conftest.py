"""Utilidades compartidas por los tests del motor ``ft_vectorbt`` (Fase 1).

Construyen velas canónicas (``Int64``/``Float32``) y ``Signals`` (``Boolean``) pequeñas para
los casos calculados a mano, y encadenan los tres puntos públicos de ``engine.py``
(``to_framework_input`` → ``simulate`` → ``from_framework_output``) como hace
``VectorbtEngine.run``.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl
from finances_core.backtest.engine_api import CostModel, EngineOutput, Signals

from finances_backtester.ft_vectorbt.engine import (
    from_framework_output,
    simulate,
    to_framework_input,
)

#: Inicio de las velas sintéticas (ms UTC) y paso de una hora.
T0_MS = 1_700_000_000_000
STEP_MS = 3_600_000
NO_COSTS = CostModel(fees_bps=0.0, slippage_bps=0.0)


def times(n: int) -> list[int]:
    """``open_time_ms`` de ``n`` barras horarias consecutivas."""
    return [T0_MS + i * STEP_MS for i in range(n)]


def make_candles(
    open_: Sequence[float],
    close: Sequence[float],
    open_time_ms: Sequence[int] | None = None,
) -> pl.DataFrame:
    """Velas RF-4 canónicas; ``high``/``low`` envuelven ``open``/``close``."""
    n = len(open_)
    stamps = list(open_time_ms) if open_time_ms is not None else times(n)
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series(stamps, dtype=pl.Int64),
            "open": pl.Series(list(open_), dtype=pl.Float32),
            "high": pl.Series([max(o, c) for o, c in zip(open_, close, strict=True)]).cast(
                pl.Float32
            ),
            "low": pl.Series([min(o, c) for o, c in zip(open_, close, strict=True)]).cast(
                pl.Float32
            ),
            "close": pl.Series(list(close), dtype=pl.Float32),
        }
    )


def make_signals(n: int, entries: Sequence[int] = (), exits: Sequence[int] = ()) -> Signals:
    """``Signals`` con ``True`` en las barras indicadas (señal al cierre de ``t``)."""
    return Signals(
        entries=pl.Series("entries", [i in entries for i in range(n)], dtype=pl.Boolean),
        exits=pl.Series("exits", [i in exits for i in range(n)], dtype=pl.Boolean),
    )


def run_pipeline(
    candles: pl.DataFrame,
    signals: Signals,
    costs: CostModel = NO_COSTS,
    size_fraction: float = 1.0,
    initial_cash: float = 1000.0,
) -> EngineOutput:
    """Los tres puntos públicos de ``engine.py`` encadenados (igual que ``run``)."""
    inp = to_framework_input(candles, signals)
    res = simulate(inp, costs, size_fraction, initial_cash)
    return from_framework_output(res, candles["open_time_ms"])
