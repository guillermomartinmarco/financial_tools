"""Lógica de la estrategia Inside Bar Breakout.

Un "inside bar" es una barra cuyo rango está contenido en el rango de la barra anterior.
Se entra en largo si la barra actual rompe el máximo de la barra inside bar previa.
Se sale si rompe el mínimo de la barra inside bar previa.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import position_from_events


class InsideBarBreakoutParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InsideBarBreakout(BaseStrategy[InsideBarBreakoutParams]):
    kind = "inside_bar_breakout"
    version = "1.0.0"
    description = "Compra la ruptura al alza de un inside bar y vende la ruptura a la baja."
    Params = InsideBarBreakoutParams
    default_params = {}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ()

    @staticmethod
    def warmup(params: InsideBarBreakoutParams) -> int:
        return 2

    @staticmethod
    def signals(candles: pl.DataFrame, params: InsideBarBreakoutParams) -> pl.Series:
        high = pl.col("high")
        low = pl.col("low")
        close = pl.col("close")

        is_inside = (high < high.shift(1)) & (low > low.shift(1))

        entry = is_inside.shift(1) & (close > high.shift(1))
        exit_ = is_inside.shift(1) & (close < low.shift(1))

        return candles.select(
            position_from_events(entry.fill_null(False), exit_.fill_null(False))
        ).to_series()
