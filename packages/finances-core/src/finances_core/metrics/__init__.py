"""Catálogo de métricas neutral (`finances_core.metrics`).

Hogar transversal de cálculo de métricas, independiente del productor de datos (backtester,
bot vivo/paper/sim, analizador de trades históricos) y del dominio (trading hoy; inversión a
futuro). Expone el contrato canónico ``trades``/``equity`` (:mod:`finances_core.metrics.frames`)
y, a medida que se implementen los slices, el catálogo de métricas de trading y su registro de
objetivos.
"""

from __future__ import annotations

from finances_core.metrics.frames import (
    EQUITY_SCHEMA,
    EXIT_REASON_DOMAIN,
    EXIT_REASON_END_OF_DATA,
    EXIT_REASON_SIGNAL,
    SIDE_DOMAIN,
    SIDE_LONG,
    TRADES_SCHEMA,
)

__all__ = [
    "EQUITY_SCHEMA",
    "EXIT_REASON_DOMAIN",
    "EXIT_REASON_END_OF_DATA",
    "EXIT_REASON_SIGNAL",
    "SIDE_DOMAIN",
    "SIDE_LONG",
    "TRADES_SCHEMA",
]
