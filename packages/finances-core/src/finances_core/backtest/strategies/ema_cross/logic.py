"""Estrategia integrada ``ema_cross``: cruce de medias móviles exponenciales (RF-2, L2).

``entry[t]`` = ``EMA_fast(t-1) <= EMA_slow(t-1)`` y ``EMA_fast(t) > EMA_slow(t)``;
``exit[t]`` = ``EMA_fast(t-1) >= EMA_slow(t-1)`` y ``EMA_fast(t) < EMA_slow(t)``.
Como ``sma_cross`` pero con EMA (reacciona antes al precio reciente). Largo/plano.
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


class EmaCrossParams(BaseModel):
    """``2 <= fast < slow <= 10000`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    fast: int = Field(default=12, ge=2, le=10_000, description="Barras de la EMA rápida")
    slow: int = Field(default=26, ge=3, le=10_000, description="Barras de la EMA lenta")

    @model_validator(mode="after")
    def _fast_below_slow(self) -> Self:
        if self.fast >= self.slow:
            msg = f"fast ({self.fast}) debe ser menor que slow ({self.slow})"
            raise ValueError(msg)
        return self


class EmaCross(BaseStrategy[EmaCrossParams]):
    kind = "ema_cross"
    version = "1.0.0"
    description = "Cruce de la media móvil exponencial rápida sobre la lenta (largo/plano)."
    Params = EmaCrossParams
    default_params = {"fast": 12, "slow": 26}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: Parámetros cortos para que la verificación de autor vea muchos cruces (plan §3.8).
    check_params = ({"fast": 3, "slow": 8},)

    @staticmethod
    def warmup(params: EmaCrossParams) -> int:
        # EMA_slow definida desde t = slow-1; el cruce necesita t y t-1 -> t >= slow.
        return params.slow

    @staticmethod
    def signals(candles: pl.DataFrame, params: EmaCrossParams) -> pl.Series:
        fast = ema(pl.col("close"), params.fast)
        slow = ema(pl.col("close"), params.slow)
        return candles.select(
            position_from_events(cross_above(fast, slow), cross_below(fast, slow))
        ).to_series()
