"""Plantilla de estrategia local (RF-22): cruce del cierre sobre su media móvil.

Copia la carpeta ``_template`` a ``FT_STRATEGIES_DIR/<kind>/``, cambia ``kind`` para que
coincida con el nombre de la carpeta y adapta ``Params``, ``warmup`` y ``signals``. La
guía completa está en ``docs/guides/strategy-authoring.md``.

Reglas que comprueba el núcleo (RF-16…RF-20):

- Solo se importan ``polars``, ``pydantic``, ``math``, ``typing``, ``dataclasses``,
  ``enum``, ``functools``, ``itertools``, ``collections``, ``decimal``,
  ``finances_core.backtest.strategies`` (incluido ``_lib``) y módulos hermanos de esta
  carpeta. Nada de ``pandas``, NumPy, doble precisión, ``open``, ``eval``…
- ``signals`` es pura y determinista: solo lee ``candles`` (``open_time_ms`` ``Int64``;
  ``open``/``high``/``low``/``close``/``volume`` ``Float32``), no guarda estado y nunca mira barras
  futuras (nada de ``shift`` negativo).
"""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, Signals
from finances_core.backtest.strategies._lib import cross_above, cross_below, sma


class MiEstrategiaParams(BaseModel):
    """Fuente única de tipos y rangos: el JSON Schema de ``kinds`` se genera de aquí."""

    # extra="forbid" es obligatorio: un parámetro desconocido es un error (invalid_params).
    model_config = ConfigDict(extra="forbid")

    # Cada campo necesita una fila en la tabla «Diccionario de parámetros» del README.
    window: int = Field(default=20, ge=2, le=1000, description="Barras de la media")


class MiEstrategia(BaseStrategy[MiEstrategiaParams]):
    # Igual al nombre de la carpeta: ^[a-z][a-z0-9_]{2,39}$.
    kind = "mi_estrategia"
    # SemVer de la lógica: súbela al cambiar las señales.
    version = "0.1.0"
    # Una sola línea.
    description = "Plantilla: compra cuando el cierre cruza su media móvil al alza."
    Params = MiEstrategiaParams
    # Debe validar contra Params y coincidir con la columna «Defecto» del README.
    default_params = {"window": 20}

    @staticmethod
    def warmup(params: MiEstrategiaParams) -> int:
        # Primer t con señal posible: SMA definida en t y t-1 -> t >= window.
        return params.window

    @staticmethod
    def signals(candles: pl.DataFrame, params: MiEstrategiaParams) -> Signals:
        close = pl.col("close")
        average = sma(close, params.window)
        frame = candles.select(
            entries=cross_above(close, average),
            exits=cross_below(close, average),
        )
        # Dos pl.Series Boolean de longitud candles.height (sin nulos tras warmup).
        return Signals(entries=frame["entries"], exits=frame["exits"])
