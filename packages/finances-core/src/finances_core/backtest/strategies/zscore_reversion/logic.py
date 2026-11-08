"""Estrategia integrada ``zscore_reversion``: reversión a la media estadística vía Z-Score (RF-2).

``entry[t]`` = ``zscore(close, n)[t] < -threshold`` (sobreventa);
``exit[t]`` = ``zscore(close, n)[t] > 0`` (regreso a la media).
Largo/plano.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import position_from_events, zscore


class ZscoreReversionParams(BaseModel):
    """``2 <= n <= 10000`` y ``0.0 <= threshold <= 10.0`` (RF-2)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=20, ge=2, le=10_000, description="Periodo para el cálculo del Z-Score")
    threshold: float = Field(
        default=2.0, ge=0.0, le=10.0, description="Umbral de desviación estándar para entrada"
    )


class ZscoreReversion(BaseStrategy[ZscoreReversionParams]):
    kind = "zscore_reversion"
    version = "1.0.0"
    description = "Compra Z-Score bajo un umbral negativo y vende al volver a la media."
    Params = ZscoreReversionParams
    default_params = {"n": 20, "threshold": 2.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 5, "threshold": 1.0},)

    @staticmethod
    def warmup(params: ZscoreReversionParams) -> int:
        # zscore requiere n barras para el primer valor en t = n-1.
        return params.n

    @staticmethod
    def signals(candles: pl.DataFrame, params: ZscoreReversionParams) -> pl.Series:
        z = zscore(pl.col("close"), params.n)
        threshold = params.threshold
        return candles.select(position_from_events(z < -threshold, z > 0)).to_series()
