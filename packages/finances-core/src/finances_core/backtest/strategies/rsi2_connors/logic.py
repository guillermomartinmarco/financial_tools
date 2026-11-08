"""Estrategia integrada ``rsi2_connors``: reversión a la media RSI(2) de Larry Connors (RF-2).

``entry[t]`` = ``rsi_wilder(close, period)[t] < lower`` (sobreventa profunda);
``exit[t]`` = ``rsi_wilder(close, period)[t] > upper`` (zona neutral/sobrecompra).
Largo/plano.
"""

from __future__ import annotations

from typing import Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    position_from_events,
    rsi_wilder,
)


class Rsi2ConnorsParams(BaseModel):
    """``1 <= period <= 1000`` y ``0.0 <= lower < upper <= 100.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    period: int = Field(default=2, ge=1, le=1000, description="Periodo del RSI de Wilder")
    lower: float = Field(
        default=10.0, ge=0.0, lt=100.0, description="Umbral de sobreventa profunda"
    )
    upper: float = Field(default=70.0, gt=0.0, le=100.0, description="Umbral de salida")

    @model_validator(mode="after")
    def _lower_below_upper(self) -> Self:
        if self.lower >= self.upper:
            msg = f"lower ({self.lower}) debe ser menor que upper ({self.upper})"
            raise ValueError(msg)
        return self


class Rsi2Connors(BaseStrategy[Rsi2ConnorsParams]):
    kind = "rsi2_connors"
    version = "1.0.0"
    description = "Compra sobreventa profunda con RSI(2) y vende al cruzar el umbral superior."
    Params = Rsi2ConnorsParams
    default_params = {"period": 2, "lower": 10.0, "upper": 70.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"period": 2, "lower": 20.0, "upper": 50.0},)

    @staticmethod
    def warmup(params: Rsi2ConnorsParams) -> int:
        # rsi_wilder requiere period + 1 para estabilizarse según rsi_reversion.
        return params.period + 1

    @staticmethod
    def signals(candles: pl.DataFrame, params: Rsi2ConnorsParams) -> pl.Series:
        rsi = rsi_wilder(pl.col("close"), params.period)
        return candles.select(
            position_from_events(rsi < params.lower, rsi > params.upper)
        ).to_series()
