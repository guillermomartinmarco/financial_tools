"""Estrategia integrada ``high_breakout``: ruptura del máximo de cierre (RF-2, L2).

Estilo Turtle / 52-week-high sobre el cierre: entra cuando ``close`` supera el máximo de
los ``window`` cierres previos y sale cuando perfora el mínimo de los ``window`` cierres
previos. El canal **excluye** la barra actual. Largo/plano por defecto; con
``allow_short=True`` (Enmienda G3) la ruptura bajista abre corto.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    position_from_events,
    rolling_max_prev,
    rolling_min_prev,
)
from finances_core.backtest.strategies.signals import position_from_long_short


class HighBreakoutParams(BaseModel):
    """``2 <= window <= 10000`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    window: int = Field(default=20, ge=2, le=10_000, description="Barras del canal de cierre")
    allow_short: bool = Field(
        default=False, description="La ruptura bajista abre corto en vez de aplanar (Enmienda G3)"
    )


class HighBreakout(BaseStrategy[HighBreakoutParams]):
    kind = "high_breakout"
    version = "1.1.0"
    description = (
        "Ruptura del máximo de cierre de window barras; sale bajo el mínimo (largo/plano)."
    )
    Params = HighBreakoutParams
    default_params = {"window": 20}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: El segundo set ejercita allow_short=True (contrato + anti-anticipación, Enmienda G3).
    check_params = ({"window": 3}, {"window": 3, "allow_short": True})

    @staticmethod
    def warmup(params: HighBreakoutParams) -> int:
        # rolling_max_prev/min_prev(close, window) existen desde t = window.
        return params.window

    @staticmethod
    def signals(candles: pl.DataFrame, params: HighBreakoutParams) -> pl.Series:
        close = pl.col("close")
        highest = rolling_max_prev(close, params.window)
        lowest = rolling_min_prev(close, params.window)
        # Estado sostenido: largo tras romper el máximo previo, plano tras perder el mínimo.
        bullish, bearish = close > highest, close < lowest
        if params.allow_short:
            frame = candles.select(bullish=bullish, bearish=bearish)
            return position_from_long_short(
                enter_long=frame["bullish"],
                exit_long=frame["bearish"],
                enter_short=frame["bearish"],
                exit_short=frame["bullish"],
            )
        return candles.select(position_from_events(bullish, bearish)).to_series()
