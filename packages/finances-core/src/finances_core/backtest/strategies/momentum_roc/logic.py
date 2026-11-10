"""Estrategia integrada ``momentum_roc``: momentum por tasa de cambio (RF-2, L2).

Posición sostenida según el signo del momentum: largo mientras ``roc(close, n)`` supera
``threshold``; plano cuando cae por debajo. Momentum de series temporales (*time-series
/ absolute momentum*). Largo/plano por defecto; con ``allow_short=True`` (Enmienda G3)
el momentum negativo simétrico (``roc < -threshold``) abre corto en vez de solo aplanar
(a diferencia de las demás estrategias de esta familia, el espejo corto usa el umbral
**negado**, no la condición de salida existente: ``threshold`` sigue siendo el umbral de
salida asimétrico cuando ``allow_short=False``).
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, MetricsTarget
from finances_core.backtest.strategies._lib import position_from_events, roc
from finances_core.backtest.strategies.signals import position_from_long_short


class MomentumRocParams(BaseModel):
    """``1 <= n <= 10000`` y ``threshold`` como fracción (``0.05`` = 5 %)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(default=20, ge=1, le=10_000, description="Barras de la tasa de cambio")
    threshold: float = Field(
        default=0.0, ge=-1.0, le=10.0, description="Umbral de momentum (fracción)"
    )
    allow_short: bool = Field(
        default=False,
        description="Momentum negativo simétrico (roc < -threshold) abre corto (Enmienda G3)",
    )


class MomentumRoc(BaseStrategy[MomentumRocParams]):
    kind = "momentum_roc"
    version = "1.1.0"
    description = "Largo mientras la tasa de cambio de n barras supera un umbral (largo/plano)."
    Params = MomentumRocParams
    default_params = {"n": 20, "threshold": 0.0}
    metrics_target = MetricsTarget(metric="sharpe", direction="max")
    #: El segundo set ejercita allow_short=True (contrato + anti-anticipación, Enmienda G3).
    check_params = ({"n": 3, "threshold": 0.0}, {"n": 3, "threshold": 0.0, "allow_short": True})

    @staticmethod
    def warmup(params: MomentumRocParams) -> int:
        # roc(close, n) existe desde t = n.
        return params.n

    @staticmethod
    def signals(candles: pl.DataFrame, params: MomentumRocParams) -> pl.Series:
        momentum = roc(pl.col("close"), params.n)
        threshold = params.threshold
        if params.allow_short:
            # Espejo simétrico alrededor de 0 (no el umbral asimétrico de abajo): largo
            # con momentum fuerte positivo, corto con momentum fuerte negativo, cada
            # salida al cruzar de vuelta el umbral del lado contrario.
            frame = candles.select(
                enter_long=momentum > threshold,
                exit_long=momentum < -threshold,
                enter_short=momentum < -threshold,
                exit_short=momentum > threshold,
            )
            return position_from_long_short(
                frame["enter_long"], frame["exit_long"], frame["enter_short"], frame["exit_short"]
            )
        # Estado sostenido: entra cuando el momentum supera el umbral, sale cuando cae por
        # debajo; en el empate exacto mantiene la posición (forward-fill).
        return candles.select(
            position_from_events(momentum > threshold, momentum < threshold)
        ).to_series()
