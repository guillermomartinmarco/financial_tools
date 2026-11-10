"""Lógica de la estrategia OBV Trend.

Se entra en largo cuando el On-Balance Volume (OBV) cruza al alza su propia media
móvil simple (SMA). Se sale cuando el OBV cruza a la baja dicha media. Largo/plano por
defecto; con ``allow_short=True`` (Enmienda G3) el cruce a la baja abre corto.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    cross_above,
    cross_below,
    obv,
    position_from_events,
    sma,
)
from finances_core.backtest.strategies.signals import position_from_long_short


class ObvTrendParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(20, ge=2, le=10_000)
    allow_short: bool = Field(
        default=False, description="El cruce a la baja abre corto en vez de aplanar (Enmienda G3)"
    )


class ObvTrend(BaseStrategy[ObvTrendParams]):
    kind = "obv_trend"
    version = "1.1.0"
    description = "Sigue la tendencia del volumen mediante el cruce del OBV con su SMA."
    Params = ObvTrendParams
    default_params = {"n": 20}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 5}, {"n": 5, "allow_short": True})

    @staticmethod
    def warmup(params: ObvTrendParams) -> int:
        return params.n

    @staticmethod
    def signals(candles: pl.DataFrame, params: ObvTrendParams) -> pl.Series:
        obv_series = obv(pl.col("close"), pl.col("volume"))
        obv_avg = sma(obv_series, params.n)

        entry = cross_above(obv_series, obv_avg)
        exit_ = cross_below(obv_series, obv_avg)
        if params.allow_short:
            frame = candles.select(entry=entry, exit_=exit_)
            return position_from_long_short(
                enter_long=frame["entry"],
                exit_long=frame["exit_"],
                enter_short=frame["exit_"],
                exit_short=frame["entry"],
            )
        return candles.select(position_from_events(entry, exit_)).to_series()
