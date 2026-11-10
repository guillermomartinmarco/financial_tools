"""Estrategia integrada ``trend_filtered_reversion``: combinador tendencia + RSI (RF-2).

``trend[t]`` = ``sma(close, trend_n)[t]``.
``rsi[t]`` = ``rsi_wilder(close, rsi_period)[t]``.
``entry[t]`` = ``close[t] > trend[t]`` Y ``rsi[t] < rsi_lower``.
``exit[t]`` = ``rsi[t] > rsi_upper`` O ``close[t] < trend[t]``.
Largo/plano por defecto; con ``allow_short=True`` (Enmienda G3) el espejo bajista
(tendencia a la baja + rebote RSI, reflejo ``100-x`` de ``rsi_lower``/``rsi_upper``)
abre corto.
"""

from __future__ import annotations

from typing import Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    position_from_events,
    rsi_wilder,
    sma,
)
from finances_core.backtest.strategies.signals import position_from_long_short


class TrendFilteredReversionParams(BaseModel):
    """``2 <= trend_n <= 10000``, ``1 <= rsi_period <= 1000``,
    ``0.0 <= rsi_lower < rsi_upper <= 100.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    trend_n: int = Field(default=200, ge=2, le=10_000, description="Periodo de la SMA de tendencia")
    rsi_period: int = Field(default=2, ge=1, le=1000, description="Periodo del RSI de Wilder")
    rsi_lower: float = Field(default=10.0, ge=0.0, lt=100.0, description="Umbral de sobreventa")
    rsi_upper: float = Field(default=70.0, gt=0.0, le=100.0, description="Umbral de salida")
    allow_short: bool = Field(
        default=False,
        description="Tendencia bajista + rebote RSI (reflejo 100-x) abre corto (Enmienda G3)",
    )

    @model_validator(mode="after")
    def _lower_below_upper(self) -> Self:
        if self.rsi_lower >= self.rsi_upper:
            msg = f"rsi_lower ({self.rsi_lower}) debe ser menor que rsi_upper ({self.rsi_upper})"
            raise ValueError(msg)
        return self


class TrendFilteredReversion(BaseStrategy[TrendFilteredReversionParams]):
    kind = "trend_filtered_reversion"
    version = "1.1.0"
    description = (
        "Compra dips de RSI en tendencia alcista y vende por momentum o ruptura de tendencia."
    )
    Params = TrendFilteredReversionParams
    default_params = {"trend_n": 200, "rsi_period": 2, "rsi_lower": 10.0, "rsi_upper": 70.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: Parámetros cortos para que la verificación de autor genere señales en 600 barras.
    check_params = (
        {"trend_n": 5, "rsi_period": 2, "rsi_lower": 20.0, "rsi_upper": 50.0},
        {"trend_n": 5, "rsi_period": 2, "rsi_lower": 20.0, "rsi_upper": 50.0, "allow_short": True},
    )

    @staticmethod
    def warmup(params: TrendFilteredReversionParams) -> int:
        # sma(trend_n) nula para t < trend_n-1.
        # rsi_wilder(rsi_period) nula para t < rsi_period.
        return max(params.trend_n - 1, params.rsi_period)

    @staticmethod
    def signals(candles: pl.DataFrame, params: TrendFilteredReversionParams) -> pl.Series:
        close = pl.col("close")
        trend = sma(close, params.trend_n)
        rsi = rsi_wilder(close, params.rsi_period)

        enter_long = ((close > trend) & (rsi < params.rsi_lower)).fill_null(value=False)
        exit_long = ((rsi > params.rsi_upper) | (close < trend)).fill_null(value=False)
        if params.allow_short:
            enter_short = ((close < trend) & (rsi > (100.0 - params.rsi_lower))).fill_null(
                value=False
            )
            exit_short = ((rsi < (100.0 - params.rsi_upper)) | (close > trend)).fill_null(
                value=False
            )
            frame = candles.select(
                enter_long=enter_long,
                exit_long=exit_long,
                enter_short=enter_short,
                exit_short=exit_short,
            )
            return position_from_long_short(
                frame["enter_long"], frame["exit_long"], frame["enter_short"], frame["exit_short"]
            )
        return candles.select(position_from_events(enter_long, exit_long)).to_series()
