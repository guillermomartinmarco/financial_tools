"""Lógica de la estrategia VWAP Reversion.

Se entra en largo cuando el precio de cierre está significativamente por debajo del VWAP rodante.
Se sale cuando el precio recupera el nivel del VWAP. Largo/plano por defecto; con
``allow_short=True`` (Enmienda G3) el precio significativamente por encima del VWAP
abre corto.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import position_from_events, vwap
from finances_core.backtest.strategies.signals import position_from_long_short


class VwapReversionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(20, ge=2, le=10_000)
    threshold: float = Field(0.02, ge=0.0, le=1.0)
    allow_short: bool = Field(
        default=False,
        description="El precio por encima del VWAP (simétrico) abre corto (Enmienda G3)",
    )


class VwapReversion(BaseStrategy[VwapReversionParams]):
    kind = "vwap_reversion"
    version = "1.1.0"
    description = "Compra cuando el precio cae significativamente por debajo del VWAP rodante."
    Params = VwapReversionParams
    default_params = {"n": 20, "threshold": 0.02}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    check_params = ({"n": 5, "threshold": 0.01}, {"n": 5, "threshold": 0.01, "allow_short": True})

    @staticmethod
    def warmup(params: VwapReversionParams) -> int:
        return params.n - 1

    @staticmethod
    def signals(candles: pl.DataFrame, params: VwapReversionParams) -> pl.Series:
        vw = vwap(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), params.n)
        close = pl.col("close")
        entry = (close < vw * (1.0 - params.threshold)).fill_null(False)
        exit_ = (close > vw).fill_null(False)
        if params.allow_short:
            enter_short = (close > vw * (1.0 + params.threshold)).fill_null(False)
            exit_short = (close < vw).fill_null(False)
            frame = candles.select(
                enter_long=entry, exit_long=exit_, enter_short=enter_short, exit_short=exit_short
            )
            return position_from_long_short(
                frame["enter_long"], frame["exit_long"], frame["enter_short"], frame["exit_short"]
            )
        return candles.select(position_from_events(entry, exit_)).to_series()
