"""Lógica de la estrategia Inside Bar Breakout.

Un "inside bar" es una barra cuyo rango está contenido en el rango de la barra anterior.
Se entra en largo si la barra actual rompe el máximo de la barra inside bar previa.
Se sale si rompe el mínimo de la barra inside bar previa. Largo/plano por defecto; con
``allow_short=True`` (Enmienda G3) la ruptura a la baja abre corto.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import position_from_events
from finances_core.backtest.strategies.signals import position_from_long_short


class InsideBarBreakoutParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_short: bool = Field(
        default=False,
        description="La ruptura a la baja abre corto en vez de aplanar (Enmienda G3)",
    )


class InsideBarBreakout(BaseStrategy[InsideBarBreakoutParams]):
    kind = "inside_bar_breakout"
    version = "1.1.0"
    description = "Compra la ruptura al alza de un inside bar y vende la ruptura a la baja."
    Params = InsideBarBreakoutParams
    default_params = {}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"allow_short": True},)

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
        bullish = entry.fill_null(False)
        bearish = exit_.fill_null(False)
        if params.allow_short:
            frame = candles.select(bullish=bullish, bearish=bearish)
            return position_from_long_short(
                enter_long=frame["bullish"],
                exit_long=frame["bearish"],
                enter_short=frame["bearish"],
                exit_short=frame["bullish"],
            )
        return candles.select(position_from_events(bullish, bearish)).to_series()
