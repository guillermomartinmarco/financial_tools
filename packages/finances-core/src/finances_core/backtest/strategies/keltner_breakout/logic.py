"""Estrategia integrada ``keltner_breakout``: ruptura del canal de Keltner (RF-2).

``middle[t]`` = ``ema(close, n)[t]`` y ``channel[t]`` = ``k * atr(high, low, close, n)[t]``.
``upper[t]`` = ``middle[t] + channel[t]``; ``lower[t]`` = ``middle[t] - channel[t]``.
``entry[t]`` = ``close[t] > upper[t]``; ``exit[t]`` = ``close[t] < lower[t]``.
Largo/plano por defecto; con ``allow_short=True`` (Enmienda G3) la ruptura inferior
abre corto.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    atr,
    ema,
    position_from_events,
)
from finances_core.backtest.strategies.signals import position_from_long_short


class KeltnerBreakoutParams(BaseModel):
    """``2 <= n <= 10000`` y ``0.5 <= k <= 10.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=20, ge=2, le=10_000, description="Periodo de la EMA y el ATR")
    k: float = Field(default=2.0, ge=0.5, le=10.0, description="Multiplicador del ATR")
    allow_short: bool = Field(
        default=False,
        description="La ruptura inferior abre corto en vez de aplanar (Enmienda G3)",
    )


class KeltnerBreakout(BaseStrategy[KeltnerBreakoutParams]):
    kind = "keltner_breakout"
    version = "1.1.0"
    description = "Compra la ruptura del canal superior de Keltner y vende la del inferior."
    Params = KeltnerBreakoutParams
    default_params = {"n": 20, "k": 2.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 5, "k": 1.0}, {"n": 5, "k": 1.0, "allow_short": True})

    @staticmethod
    def warmup(params: KeltnerBreakoutParams) -> int:
        # ema(n) nula para t < n-1; atr(n) nula para t < n.
        return params.n

    @staticmethod
    def signals(candles: pl.DataFrame, params: KeltnerBreakoutParams) -> pl.Series:
        close = pl.col("close")
        high = pl.col("high")
        low = pl.col("low")

        middle = ema(close, params.n)
        channel = params.k * atr(high, low, close, params.n)
        upper = middle + channel
        lower = middle - channel
        bullish = (close > upper).fill_null(value=False)
        bearish = (close < lower).fill_null(value=False)
        if params.allow_short:
            frame = candles.select(bullish=bullish, bearish=bearish)
            return position_from_long_short(
                enter_long=frame["bullish"],
                exit_long=frame["bearish"],
                enter_short=frame["bearish"],
                exit_short=frame["bullish"],
            )

        return candles.select(position_from_events(bullish, bearish)).to_series()
