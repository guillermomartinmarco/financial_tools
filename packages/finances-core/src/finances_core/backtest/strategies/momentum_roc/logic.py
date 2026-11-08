"""Estrategia integrada ``momentum_roc``: momentum por tasa de cambio (RF-2, L2).

Posición sostenida según el signo del momentum: largo mientras ``roc(close, n)`` supera
``threshold``; plano cuando cae por debajo. Momentum de series temporales (*time-series
/ absolute momentum*). Largo/plano.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import position_from_events, roc


class MomentumRocParams(BaseModel):
    """``1 <= n <= 10000`` y ``threshold`` como fracción (``0.05`` = 5 %)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=20, ge=1, le=10_000, description="Barras de la tasa de cambio")
    threshold: float = Field(
        default=0.0, ge=-1.0, le=10.0, description="Umbral de momentum (fracción)"
    )


class MomentumRoc(BaseStrategy[MomentumRocParams]):
    kind = "momentum_roc"
    version = "1.0.0"
    description = "Largo mientras la tasa de cambio de n barras supera un umbral (largo/plano)."
    Params = MomentumRocParams
    default_params = {"n": 20, "threshold": 0.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 3, "threshold": 0.0},)

    @staticmethod
    def warmup(params: MomentumRocParams) -> int:
        # roc(close, n) existe desde t = n.
        return params.n

    @staticmethod
    def signals(candles: pl.DataFrame, params: MomentumRocParams) -> pl.Series:
        momentum = roc(pl.col("close"), params.n)
        threshold = params.threshold
        # Estado sostenido: entra cuando el momentum supera el umbral, sale cuando cae por
        # debajo; en el empate exacto mantiene la posición (forward-fill).
        return candles.select(
            position_from_events(momentum > threshold, momentum < threshold)
        ).to_series()
