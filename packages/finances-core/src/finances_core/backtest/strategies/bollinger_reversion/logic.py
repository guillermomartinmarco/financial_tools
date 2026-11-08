"""Estrategia integrada ``bollinger_reversion``: reversión a Bandas de Bollinger (RF-2).

``entry[t]`` = ``close[t] < lower[t]`` (sobreventa);
``exit[t]`` = ``close[t] > middle[t]`` (regreso a la media).
Largo/plano.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import (
    position_from_events,
    rolling_std_stable,
    sma,
)


class BollingerReversionParams(BaseModel):
    """``2 <= n <= 10000`` y ``0.5 <= k <= 10.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=20, ge=2, le=10_000, description="Periodo de la media móvil")
    k: float = Field(
        default=2.0, ge=0.5, le=10.0, description="Multiplicador de la desviación estándar"
    )


class BollingerReversion(BaseStrategy[BollingerReversionParams]):
    kind = "bollinger_reversion"
    version = "1.0.0"
    description = "Compra bajo la banda inferior de Bollinger y vende en la media (largo/plano)."
    Params = BollingerReversionParams
    default_params = {"n": 20, "k": 2.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 5, "k": 1.0},)

    @staticmethod
    def warmup(params: BollingerReversionParams) -> int:
        # sma y rolling_std_stable requieren n barras para el primer valor en t = n-1.
        return params.n

    @staticmethod
    def signals(candles: pl.DataFrame, params: BollingerReversionParams) -> pl.Series:
        close = pl.col("close")
        middle = sma(close, params.n)
        std = rolling_std_stable(close, params.n)
        lower = middle - params.k * std
        return candles.select(position_from_events(close < lower, close > middle)).to_series()
