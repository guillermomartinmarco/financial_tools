"""Lógica de la estrategia Channel Breakout (Soporte/Resistencia Asimétrico).

Se entra en largo cuando el precio rompe la resistencia (máximo de los últimos
``entry_window`` periodos) y se sale cuando rompe el soporte (mínimo de los últimos
``exit_window`` periodos). Largo/plano por defecto; con ``allow_short=True``
(Enmienda G3) la ruptura del soporte abre corto.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    position_from_events,
    rolling_max_prev,
    rolling_min_prev,
)
from finances_core.backtest.strategies.signals import position_from_long_short


class ChannelBreakoutParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_window: int = Field(20, ge=2, le=10_000)
    exit_window: int = Field(10, ge=2, le=10_000)
    allow_short: bool = Field(
        default=False,
        description="La ruptura del soporte abre corto en vez de aplanar (Enmienda G3)",
    )


class ChannelBreakout(BaseStrategy[ChannelBreakoutParams]):
    kind = "channel_breakout"
    version = "1.1.0"
    description = (
        "Compra la ruptura de la resistencia y vende la del soporte con ventanas asimétricas."
    )
    Params = ChannelBreakoutParams
    default_params = {"entry_window": 20, "exit_window": 10}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = (
        {"entry_window": 5, "exit_window": 3},
        {"entry_window": 5, "exit_window": 3, "allow_short": True},
    )

    @staticmethod
    def warmup(params: ChannelBreakoutParams) -> int:
        return max(params.entry_window, params.exit_window)

    @staticmethod
    def signals(candles: pl.DataFrame, params: ChannelBreakoutParams) -> pl.Series:
        resistance = rolling_max_prev(pl.col("high"), params.entry_window)
        support = rolling_min_prev(pl.col("low"), params.exit_window)

        entry = pl.col("close") > resistance
        exit_ = pl.col("close") < support
        if params.allow_short:
            frame = candles.select(entry=entry, exit_=exit_)
            return position_from_long_short(
                enter_long=frame["entry"],
                exit_long=frame["exit_"],
                enter_short=frame["exit_"],
                exit_short=frame["entry"],
            )
        return candles.select(position_from_events(entry, exit_)).to_series()
