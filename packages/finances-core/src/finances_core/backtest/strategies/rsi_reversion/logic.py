"""Estrategia integrada ``rsi_reversion``: reversión a la media con el RSI de Wilder (RF-2).

``entry[t]`` = ``RSI[t-1] >= lower`` y ``RSI[t] < lower`` (entra en sobreventa);
``exit[t]`` = ``RSI[t-1] <= upper`` y ``RSI[t] > upper`` (sale en sobrecompra).
RSI definido para ``t >= period``; señales disponibles para ``t >= period + 1``.
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
    rsi_wilder,
)


class RsiReversionParams(BaseModel):
    """``2 <= period <= 1000`` y ``0 < lower < upper < 100`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    period: int = Field(default=14, ge=2, le=1000, description="Barras del RSI de Wilder")
    lower: float = Field(default=30.0, gt=0, lt=100, description="Umbral de sobreventa")
    upper: float = Field(default=70.0, gt=0, lt=100, description="Umbral de sobrecompra")

    @model_validator(mode="after")
    def _lower_below_upper(self) -> Self:
        if self.lower >= self.upper:
            msg = f"lower ({self.lower}) debe ser menor que upper ({self.upper})"
            raise ValueError(msg)
        return self


class RsiReversion(BaseStrategy[RsiReversionParams]):
    kind = "rsi_reversion"
    version = "1.0.0"
    description = "Compra la sobreventa y vende la sobrecompra del RSI de Wilder (largo/plano)."
    Params = RsiReversionParams
    default_params = {"period": 14, "lower": 30.0, "upper": 70.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: Umbrales estrechos para que la verificación de autor vea muchos cruces (plan §3.8).
    check_params = ({"period": 5, "lower": 45.0, "upper": 55.0},)

    @staticmethod
    def warmup(params: RsiReversionParams) -> int:
        return params.period + 1

    @staticmethod
    def signals(candles: pl.DataFrame, params: RsiReversionParams) -> pl.Series:
        rsi = rsi_wilder(pl.col("close"), params.period)
        return candles.select(
            position_from_events(cross_below(rsi, params.lower), cross_above(rsi, params.upper))
        ).to_series()
