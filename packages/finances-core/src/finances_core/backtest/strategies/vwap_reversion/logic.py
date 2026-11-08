"""Lógica de la estrategia VWAP Reversion.

Se entra en largo cuando el precio de cierre está significativamente por debajo del VWAP rodante.
Se sale cuando el precio recupera el nivel del VWAP.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import position_from_events, vwap


class VwapReversionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(20, ge=2, le=10_000)
    threshold: float = Field(0.02, ge=0.0, le=1.0)


class VwapReversion(BaseStrategy[VwapReversionParams]):
    kind = "vwap_reversion"
    version = "1.0.0"
    description = "Compra cuando el precio cae significativamente por debajo del VWAP rodante."
    Params = VwapReversionParams
    default_params = {"n": 20, "threshold": 0.02}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 5, "threshold": 0.01},)

    @staticmethod
    def warmup(params: VwapReversionParams) -> int:
        return params.n - 1

    @staticmethod
    def signals(candles: pl.DataFrame, params: VwapReversionParams) -> pl.Series:
        vw = vwap(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), params.n)

        entry = pl.col("close") < vw * (1.0 - params.threshold)
        exit_ = pl.col("close") > vw

        return candles.select(
            position_from_events(entry.fill_null(False), exit_.fill_null(False))
        ).to_series()
