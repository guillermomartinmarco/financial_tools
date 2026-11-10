"""Estrategia integrada ``atr_channel_breakout``: ruptura de volatilidad intrabarra (RF-2).

``entry[t]`` = ``close[t] > open[t] + k * atr(high, low, close, n)[t]``;
``exit[t]`` = ``close[t] < open[t] - k * atr(high, low, close, n)[t]``.
Largo/plano por defecto; con ``allow_short=True`` (Enmienda G3) la ruptura bajista
abre corto.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    atr,
    position_from_events,
)
from finances_core.backtest.strategies.signals import position_from_long_short


class AtrChannelBreakoutParams(BaseModel):
    """``2 <= n <= 10000`` y ``0.1 <= k <= 10.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=14, ge=2, le=10_000, description="Periodo del ATR")
    k: float = Field(default=1.0, ge=0.1, le=10.0, description="Multiplicador del ATR")
    allow_short: bool = Field(
        default=False,
        description="La ruptura bajista abre corto en vez de aplanar (Enmienda G3)",
    )


class AtrChannelBreakout(BaseStrategy[AtrChannelBreakoutParams]):
    kind = "atr_channel_breakout"
    version = "1.1.0"
    description = (
        "Compra cuando el cierre supera el open más un múltiplo del ATR "
        "y vende en el caso simétrico."
    )
    Params = AtrChannelBreakoutParams
    default_params = {"n": 14, "k": 1.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 5, "k": 0.5}, {"n": 5, "k": 0.5, "allow_short": True})

    @staticmethod
    def warmup(params: AtrChannelBreakoutParams) -> int:
        # atr(n) nula para t < n.
        return params.n

    @staticmethod
    def signals(candles: pl.DataFrame, params: AtrChannelBreakoutParams) -> pl.Series:
        close = pl.col("close")
        open_ = pl.col("open")
        high = pl.col("high")
        low = pl.col("low")

        atr_val = atr(high, low, close, params.n)

        entry = close > (open_ + params.k * atr_val)
        exit_ = close < (open_ - params.k * atr_val)
        bullish = entry.fill_null(value=False)
        bearish = exit_.fill_null(value=False)
        if params.allow_short:
            frame = candles.select(bullish=bullish, bearish=bearish)
            return position_from_long_short(
                enter_long=frame["bullish"],
                exit_long=frame["bearish"],
                enter_short=frame["bearish"],
                exit_short=frame["bullish"],
            )
        return candles.select(position_from_events(bullish, bearish)).to_series()
