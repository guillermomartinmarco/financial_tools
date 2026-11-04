"""Contrato de motor de backtesting (RF-10, RF-21, ADR-B09; plan §1.4).

Un motor **solo simula**: recibe las velas de RF-4, las señales ya normalizadas por el
núcleo (RF-16) y los costes, y devuelve las tablas canónicas de RF-6. No conoce ``kind``,
``params`` ni la estrategia resuelta, y no declara estrategias soportadas
(``supported_kinds`` no existe). El ``runner`` valida su salida con
:func:`finances_core.backtest.frames.validate_engine_output` antes de escribir nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import polars as pl

from finances_core.backtest.strategies.base import Signals
from finances_core.schemas.backtest import CostModel


@dataclass(frozen=True)
class EngineOutput:
    """Salida de :meth:`BacktestEngine.run`: tablas canónicas (RF-6) y advertencias."""

    trades: pl.DataFrame
    equity: pl.DataFrame
    warnings: list[str] = field(default_factory=list)


class BacktestEngine(Protocol):
    """Adaptador de una biblioteca de simulación (``ft_<motor>``).

    ``name`` y :meth:`version` entran en el ``run_id`` (RF-8). :meth:`run` es determinista:
    las mismas entradas producen las mismas tablas.
    """

    name: str

    def version(self) -> str:
        """Versión de la biblioteca de simulación (``engine_version``)."""
        ...

    def run(
        self,
        candles: pl.DataFrame,
        signals: Signals,
        costs: CostModel,
        size_fraction: float,
        initial_cash: float,
    ) -> EngineOutput:
        """Simula según RF-5 y devuelve ``trades``/``equity`` canónicos (RF-6).

        Args:
            candles: ``open_time_ms`` (``Int64``) y ``open``/``high``/``low``/``close``
                (``Float32``) del rango efectivo.
            signals: ``entries``/``exits`` ``Boolean`` normalizadas (RF-16), de longitud
                ``candles.height``.
            costs: comisión y deslizamiento en puntos básicos.
            size_fraction: fracción de la caja por entrada (``0 < f <= 1``).
            initial_cash: caja inicial (``> 0``).
        """
        ...
