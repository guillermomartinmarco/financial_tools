"""Estrategia integrada ``sma_cross``: cruce de medias móviles simples (RF-2, Enmienda G3).

``entry[t]`` = ``SMA_fast(t-1) <= SMA_slow(t-1)`` y ``SMA_fast(t) > SMA_slow(t)``;
``exit[t]`` = ``SMA_fast(t-1) >= SMA_slow(t-1)`` y ``SMA_fast(t) < SMA_slow(t)``.
Disponible para ``t >= slow``; los empates no generan señal. Con ``allow_short=True``
(Enmienda G3) el cruce bajista abre corto en vez de solo aplanar (familia direccional:
la reversa pasa siempre por una barra en plano, Enmienda G2b).
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
from finances_core.backtest.strategies.signals import position_from_long_short


class SmaCrossParams(BaseModel):
    """``2 <= fast < slow <= 10000`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    fast: int = Field(default=20, ge=2, le=10_000, description="Barras de la media rápida")
    slow: int = Field(default=100, ge=3, le=10_000, description="Barras de la media lenta")
    allow_short: bool = Field(
        default=False, description="El cruce bajista abre corto en vez de aplanar (Enmienda G3)"
    )

    @model_validator(mode="after")
    def _fast_below_slow(self) -> Self:
        if self.fast >= self.slow:
            msg = f"fast ({self.fast}) debe ser menor que slow ({self.slow})"
            raise ValueError(msg)
        return self


class SmaCross(BaseStrategy[SmaCrossParams]):
    kind = "sma_cross"
    version = "1.1.0"
    description = "Cruce de la media móvil simple rápida sobre la lenta (largo/plano)."
    Params = SmaCrossParams
    default_params = {"fast": 20, "slow": 100}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: Parámetros cortos para que la verificación de autor vea muchos cruces (plan §3.8);
    #: el segundo set ejercita allow_short=True (contrato + anti-anticipación, Enmienda G3).
    check_params = ({"fast": 3, "slow": 8}, {"fast": 3, "slow": 8, "allow_short": True})

    @staticmethod
    def warmup(params: SmaCrossParams) -> int:
        return params.slow

    @staticmethod
    def signals(candles: pl.DataFrame, params: SmaCrossParams) -> pl.Series:
        fast = sma(pl.col("close"), params.fast)
        slow = sma(pl.col("close"), params.slow)
        bullish, bearish = cross_above(fast, slow), cross_below(fast, slow)
        if params.allow_short:
            frame = candles.select(bullish=bullish, bearish=bearish)
            return position_from_long_short(
                enter_long=frame["bullish"],
                exit_long=frame["bearish"],
                enter_short=frame["bearish"],
                exit_short=frame["bullish"],
            )
        return candles.select(position_from_events(bullish, bearish)).to_series()
