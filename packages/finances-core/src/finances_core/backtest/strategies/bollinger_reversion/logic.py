"""Estrategia integrada ``bollinger_reversion``: reversión a Bandas de Bollinger (RF-2,
Enmienda G3).

``entry[t]`` = ``close[t] < lower[t]`` (sobreventa);
``exit[t]`` = ``close[t] > middle[t]`` (regreso a la media).
Largo/plano por defecto; con ``allow_short=True`` (Enmienda G3) también vende sobre la
banda superior (sobrecompra) y cierra el corto en la media.
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
from finances_core.backtest.strategies.signals import position_from_long_short


class BollingerReversionParams(BaseModel):
    """``2 <= n <= 10000`` y ``0.5 <= k <= 10.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=20, ge=2, le=10_000, description="Periodo de la media móvil")
    k: float = Field(
        default=2.0, ge=0.5, le=10.0, description="Multiplicador de la desviación estándar"
    )
    allow_short: bool = Field(
        default=False,
        description="Vende sobre la banda superior y cierra el corto en la media (Enmienda G3)",
    )


class BollingerReversion(BaseStrategy[BollingerReversionParams]):
    kind = "bollinger_reversion"
    version = "1.1.0"
    description = "Compra bajo la banda inferior de Bollinger y vende en la media (largo/plano)."
    Params = BollingerReversionParams
    default_params = {"n": 20, "k": 2.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: El segundo set ejercita allow_short=True (contrato + anti-anticipación, Enmienda G3).
    check_params = ({"n": 5, "k": 1.0}, {"n": 5, "k": 1.0, "allow_short": True})

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
        upper = middle + params.k * std
        if params.allow_short:
            frame = candles.select(
                enter_long=close < lower,
                exit_long=close > middle,
                enter_short=close > upper,
                exit_short=close < middle,
            )
            return position_from_long_short(
                frame["enter_long"], frame["exit_long"], frame["enter_short"], frame["exit_short"]
            )
        return candles.select(position_from_events(close < lower, close > middle)).to_series()
