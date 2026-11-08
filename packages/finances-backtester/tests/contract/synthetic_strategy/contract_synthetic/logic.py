"""Estrategia sintética de la suite de contrato (RF-10, RF-21): solo en ``tests/``.

No es una integrada: se registra como estrategia **local** con
``build_registry(local_dir=SYNTHETIC_DIR)``. Sus señales son patrones aritméticos del
índice de barra, independientes del precio, de modo que un motor solo puede pasarla si
ejecuta ``Signals`` arbitrarias sin código por estrategia.
"""

from __future__ import annotations

import polars as pl
from finances_core.backtest.strategies import BaseStrategy
from finances_core.backtest.strategies._lib import position_from_events
from pydantic import BaseModel, ConfigDict, Field


class ContractSyntheticParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_stride: int = Field(default=23, ge=2, le=1000, description="Periodo de las entradas")
    exit_stride: int = Field(default=29, ge=2, le=1000, description="Periodo de las salidas")


class ContractSynthetic(BaseStrategy[ContractSyntheticParams]):
    kind = "contract_synthetic"
    version = "1.0.0"
    description = "Señales aritméticas del índice de barra para la suite de contrato."
    Params = ContractSyntheticParams
    default_params = {"entry_stride": 23, "exit_stride": 29}

    @staticmethod
    def warmup(params: ContractSyntheticParams) -> int:
        # Sin calentamiento: hay señales desde la barra 0.
        return 0

    @staticmethod
    def signals(candles: pl.DataFrame, params: ContractSyntheticParams) -> pl.Series:
        t = pl.int_range(pl.len(), dtype=pl.Int64)
        return candles.select(
            position_from_events(
                ((t * 7) % params.entry_stride) == 0,
                ((t * 5 + 1) % params.exit_stride) == 0,
            )
        ).to_series()
