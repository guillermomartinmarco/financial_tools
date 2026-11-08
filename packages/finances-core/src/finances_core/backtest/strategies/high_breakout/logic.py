"""Estrategia integrada ``high_breakout``: ruptura del máximo de cierre (RF-2, L2).

Estilo Turtle / 52-week-high sobre el cierre: entra cuando ``close`` supera el máximo de
los ``window`` cierres previos y sale cuando perfora el mínimo de los ``window`` cierres
previos. El canal **excluye** la barra actual. Largo/plano.
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


class HighBreakoutParams(BaseModel):
    """``2 <= window <= 10000`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    window: int = Field(default=20, ge=2, le=10_000, description="Barras del canal de cierre")


class HighBreakout(BaseStrategy[HighBreakoutParams]):
    kind = "high_breakout"
    version = "1.0.0"
    description = (
        "Ruptura del máximo de cierre de window barras; sale bajo el mínimo (largo/plano)."
    )
    Params = HighBreakoutParams
    default_params = {"window": 20}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"window": 3},)

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
        return candles.select(position_from_events(close > highest, close < lowest)).to_series()
