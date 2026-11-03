"""Estrategia integrada ``donchian_breakout``: ruptura del canal de Donchian (RF-2).

``alto(t)`` = ``max(high[t-window..t-1])`` y ``bajo(t)`` = ``min(low[t-window..t-1])``
(excluyen la barra actual). ``entry[t]`` = ``close[t] > alto(t)``; ``exit[t]`` =
``close[t] < bajo(t)``. Disponible para ``t >= window``; son condiciones (estado), no
cruces: la regla común de RF-5 ignora una ``entry`` con posición abierta.
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget, Signals
from finances_core.backtest.strategies._lib import rolling_max_prev, rolling_min_prev


class DonchianBreakoutParams(BaseModel):
    """``2 <= window <= 10000`` (RF-2); la misma ventana para entrada y salida (A6)."""

    model_config = ConfigDict(extra="forbid")

    window: int = Field(default=20, ge=2, le=10_000, description="Barras del canal")


class DonchianBreakout(BaseStrategy[DonchianBreakoutParams]):
    kind = "donchian_breakout"
    version = "1.0.0"
    description = "Compra la ruptura del máximo del canal de Donchian y vende la del mínimo."
    Params = DonchianBreakoutParams
    default_params = {"window": 20}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: Ventana corta para que la verificación de autor vea muchas rupturas (plan §3.8).
    check_params = ({"window": 5},)

    @staticmethod
    def warmup(params: DonchianBreakoutParams) -> int:
        return params.window

    @staticmethod
    def signals(candles: pl.DataFrame, params: DonchianBreakoutParams) -> Signals:
        close = pl.col("close")
        high = rolling_max_prev(pl.col("high"), params.window)
        low = rolling_min_prev(pl.col("low"), params.window)
        frame = candles.select(
            entries=(close > high).fill_null(value=False),
            exits=(close < low).fill_null(value=False),
        )
        return Signals(entries=frame["entries"], exits=frame["exits"])
