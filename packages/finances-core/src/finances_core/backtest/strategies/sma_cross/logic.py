"""Estrategia integrada ``sma_cross``: cruce de medias móviles simples (RF-2).

``entry[t]`` = ``SMA_fast(t-1) <= SMA_slow(t-1)`` y ``SMA_fast(t) > SMA_slow(t)``;
``exit[t]`` = ``SMA_fast(t-1) >= SMA_slow(t-1)`` y ``SMA_fast(t) < SMA_slow(t)``.
Disponible para ``t >= slow``; los empates no generan señal.
"""

from __future__ import annotations

from typing import Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    cross_above,
    cross_below,
    position_from_events,
    sma,
)


class SmaCrossParams(BaseModel):
    """``2 <= fast < slow <= 10000`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    fast: int = Field(default=20, ge=2, le=10_000, description="Barras de la media rápida")
    slow: int = Field(default=100, ge=3, le=10_000, description="Barras de la media lenta")

    @model_validator(mode="after")
    def _fast_below_slow(self) -> Self:
        if self.fast >= self.slow:
            msg = f"fast ({self.fast}) debe ser menor que slow ({self.slow})"
            raise ValueError(msg)
        return self


class SmaCross(BaseStrategy[SmaCrossParams]):
    kind = "sma_cross"
    version = "1.0.0"
    description = "Cruce de la media móvil simple rápida sobre la lenta (largo/plano)."
    Params = SmaCrossParams
    default_params = {"fast": 20, "slow": 100}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: Parámetros cortos para que la verificación de autor vea muchos cruces (plan §3.8).
    check_params = ({"fast": 3, "slow": 8},)

    @staticmethod
    def warmup(params: SmaCrossParams) -> int:
        return params.slow

    @staticmethod
    def signals(candles: pl.DataFrame, params: SmaCrossParams) -> pl.Series:
        fast = sma(pl.col("close"), params.fast)
        slow = sma(pl.col("close"), params.slow)
        return candles.select(
            position_from_events(cross_above(fast, slow), cross_below(fast, slow))
        ).to_series()
