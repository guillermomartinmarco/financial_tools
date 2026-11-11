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
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    SIDE_DOMAIN,
    SIDE_LONG,
    TRADES_SCHEMA,
)
from finances_core.metrics.trading import (
    OBJECTIVES,
    Direction,
    EquityMetrics,
    LedgerMetrics,
    Objective,
    TradingMetrics,
    compute_equity_metrics,
    compute_ledger_metrics,
    compute_trading_metrics,
    objective_value,
)

__all__ = [
    "EQUITY_SCHEMA",
    "EXIT_REASON_DOMAIN",
    "EXIT_REASON_END_OF_DATA",
    "EXIT_REASON_SIGNAL",
    "EXIT_REASON_STOP_LOSS",
    "EXIT_REASON_TAKE_PROFIT",
    "OBJECTIVES",
    "SIDE_DOMAIN",
    "SIDE_LONG",
    "TRADES_SCHEMA",
    "Direction",
    "EquityMetrics",
    "LedgerMetrics",
    "Objective",
    "TradingMetrics",
    "compute_equity_metrics",
    "compute_ledger_metrics",
    "compute_trading_metrics",
    "objective_value",
]
