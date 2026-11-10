"""Estrategia integrada ``rsi2_connors``: reversión a la media RSI(2) de Larry Connors (RF-2).

``entry[t]`` = ``rsi_wilder(close, period)[t] < lower`` (sobreventa profunda);
``exit[t]`` = ``rsi_wilder(close, period)[t] > upper`` (zona neutral/sobrecompra).
Largo/plano por defecto; con ``allow_short=True`` (Enmienda G3) el reflejo
``100 - x`` de ``lower``/``upper`` abre corto en sobrecompra profunda.
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
from finances_core.backtest.strategies.signals import position_from_long_short


class Rsi2ConnorsParams(BaseModel):
    """``1 <= period <= 1000`` y ``0.0 <= lower < upper <= 100.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    period: int = Field(default=2, ge=1, le=1000, description="Periodo del RSI de Wilder")
    lower: float = Field(
        default=10.0, ge=0.0, lt=100.0, description="Umbral de sobreventa profunda"
    )
    upper: float = Field(default=70.0, gt=0.0, le=100.0, description="Umbral de salida")
    allow_short: bool = Field(
        default=False,
        description="Reflejo 100-x de lower/upper abre corto en sobrecompra (Enmienda G3)",
    )

    @model_validator(mode="after")
    def _lower_below_upper(self) -> Self:
        if self.lower >= self.upper:
            msg = f"lower ({self.lower}) debe ser menor que upper ({self.upper})"
            raise ValueError(msg)
        return self


class Rsi2Connors(BaseStrategy[Rsi2ConnorsParams]):
    kind = "rsi2_connors"
    version = "1.1.0"
    description = "Compra sobreventa profunda con RSI(2) y vende al cruzar el umbral superior."
    Params = Rsi2ConnorsParams
    default_params = {"period": 2, "lower": 10.0, "upper": 70.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = (
        {"period": 2, "lower": 20.0, "upper": 50.0},
        {"period": 2, "lower": 20.0, "upper": 50.0, "allow_short": True},
    )

    @staticmethod
    def warmup(params: Rsi2ConnorsParams) -> int:
        # rsi_wilder requiere period + 1 para estabilizarse según rsi_reversion.
        return params.period + 1

    @staticmethod
    def signals(candles: pl.DataFrame, params: Rsi2ConnorsParams) -> pl.Series:
        rsi = rsi_wilder(pl.col("close"), params.period)
        bullish, bearish = rsi < params.lower, rsi > params.upper
        if params.allow_short:
            frame = candles.select(
                enter_long=bullish,
                exit_long=bearish,
                enter_short=rsi > (100.0 - params.lower),
                exit_short=rsi < (100.0 - params.upper),
            )
            return position_from_long_short(
                frame["enter_long"], frame["exit_long"], frame["enter_short"], frame["exit_short"]
            )
        return candles.select(position_from_events(bullish, bearish)).to_series()
