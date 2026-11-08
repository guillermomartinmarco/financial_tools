"""Estrategia integrada ``macd_trend``: cruce de la línea MACD sobre su señal (RF-2, L2).

``MACD = EMA_fast(close) - EMA_slow(close)``; ``señal = EMA_signal(MACD)``.
``entry[t]`` = la MACD cruza **por encima** de su señal; ``exit[t]`` = la cruza por debajo.
Momentum de tendencia clásico (Appel). Largo/plano.
"""

from __future__ import annotations

from typing import Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    cross_above,
    cross_below,
    ema,
    position_from_events,
)


class MacdTrendParams(BaseModel):
    """``2 <= fast < slow <= 10000`` y ``2 <= signal <= 10000`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    fast: int = Field(default=12, ge=2, le=10_000, description="Barras de la EMA rápida")
    slow: int = Field(default=26, ge=3, le=10_000, description="Barras de la EMA lenta")
    signal: int = Field(default=9, ge=2, le=10_000, description="Barras de la EMA de la señal")

    @model_validator(mode="after")
    def _fast_below_slow(self) -> Self:
        if self.fast >= self.slow:
            msg = f"fast ({self.fast}) debe ser menor que slow ({self.slow})"
            raise ValueError(msg)
        return self


class MacdTrend(BaseStrategy[MacdTrendParams]):
    kind = "macd_trend"
    version = "1.0.0"
    description = "Cruce de la línea MACD sobre su señal EMA (momentum de tendencia, largo/plano)."
    Params = MacdTrendParams
    default_params = {"fast": 12, "slow": 26, "signal": 9}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"fast": 3, "slow": 6, "signal": 3},)

    @staticmethod
    def warmup(params: MacdTrendParams) -> int:
        # MACD definida desde t = slow-1; la EMA de la señal necesita otras signal-1
        # barras de MACD; el cruce necesita t y t-1 -> warmup = slow + signal.
        return params.slow + params.signal

    @staticmethod
    def signals(candles: pl.DataFrame, params: MacdTrendParams) -> pl.Series:
        macd = ema(pl.col("close"), params.fast) - ema(pl.col("close"), params.slow)
        signal = ema(macd, params.signal)
        return candles.select(
            position_from_events(cross_above(macd, signal), cross_below(macd, signal))
        ).to_series()
