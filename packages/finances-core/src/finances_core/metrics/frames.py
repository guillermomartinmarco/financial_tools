"""Contrato canónico neutral: ledger de operaciones (``trades``) y curva de ``equity``.

Hogar neutral (`finances_core.metrics`) del contrato de datos que consumen por igual un
backtester, un bot (vivo/paper/sim) y un analizador de trades históricos. Solo define la
forma de los datos; la **validación de la salida de un motor** (que lanza un error de
backtest) vive en :mod:`finances_core.backtest.frames`, que re-exporta estos símbolos.

Dtypes fijados (Art. 5.1 bis): ``Int64`` (ms UTC) para tiempos, ``Float32`` para magnitudes,
``Int8`` para los dominios categóricos documentados:

- ``side``: ``{1, -1}`` largo/corto (Enmienda G2b del backtester; motores previos a G2b solo
  producían ``1``);
- ``exit_reason``: ``{0}`` señal, ``{1}`` ``end_of_data`` (liquidación final).
"""

from __future__ import annotations

import polars as pl

SIDE_LONG = 1
SIDE_SHORT = -1
EXIT_REASON_SIGNAL = 0
EXIT_REASON_END_OF_DATA = 1

SIDE_DOMAIN = frozenset({SIDE_LONG, SIDE_SHORT})
EXIT_REASON_DOMAIN = frozenset({EXIT_REASON_SIGNAL, EXIT_REASON_END_OF_DATA})

#: Una fila por operación completa, ordenada por ``entry_time_ms``.
TRADES_SCHEMA = pl.Schema(
    {
        "entry_time_ms": pl.Int64,
        "exit_time_ms": pl.Int64,
        "side": pl.Int8,
        "entry_price": pl.Float32,
        "exit_price": pl.Float32,
        "size": pl.Float32,
        "fees": pl.Float32,
        "pnl": pl.Float32,
        "return": pl.Float32,
        "exit_reason": pl.Int8,
    }
)

#: Una fila por barra del rango, ordenada por ``open_time_ms``.
EQUITY_SCHEMA = pl.Schema(
    {
        "open_time_ms": pl.Int64,
        "equity": pl.Float32,
        "drawdown": pl.Float32,
    }
)

__all__ = [
    "EQUITY_SCHEMA",
    "EXIT_REASON_DOMAIN",
    "EXIT_REASON_END_OF_DATA",
    "EXIT_REASON_SIGNAL",
    "SIDE_DOMAIN",
    "SIDE_LONG",
    "SIDE_SHORT",
    "TRADES_SCHEMA",
]
