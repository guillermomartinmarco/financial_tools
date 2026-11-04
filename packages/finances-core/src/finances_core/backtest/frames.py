"""Tablas canónicas ``trades`` y ``equity`` de RF-6 y su validación (Art. 5, plan §1.3).

Los dtypes quedan fijados aquí: ``Int64`` (ms UTC) para tiempos, ``Float32`` para
magnitudes e ``Int8`` para los dominios categóricos documentados (Art. 5.1 bis):

- ``side``: ``{1}`` largo (``-1`` reservado para cortos, fuera del MVP);
- ``exit_reason``: ``{0}`` señal, ``{1}`` ``end_of_data`` (liquidación final, A5).

:func:`validate_engine_output` rechaza con :class:`OutputOutOfRangeError` cualquier salida
fuera de contrato; nunca trunca ni corrige valores.
"""

from __future__ import annotations

from typing import Protocol

import polars as pl

from finances_core.backtest.errors import OutputOutOfRangeError

SIDE_LONG = 1
EXIT_REASON_SIGNAL = 0
EXIT_REASON_END_OF_DATA = 1

SIDE_DOMAIN = frozenset({SIDE_LONG})
EXIT_REASON_DOMAIN = frozenset({EXIT_REASON_SIGNAL, EXIT_REASON_END_OF_DATA})

#: Una fila por operación completa, ordenada por ``entry_time_ms`` (RF-6).
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

#: Una fila por barra del rango, ordenada por ``open_time_ms`` (RF-6).
EQUITY_SCHEMA = pl.Schema(
    {
        "open_time_ms": pl.Int64,
        "equity": pl.Float32,
        "drawdown": pl.Float32,
    }
)


class EngineTables(Protocol):
    """Lo que :func:`validate_engine_output` necesita de un ``EngineOutput``."""

    @property
    def trades(self) -> pl.DataFrame: ...

    @property
    def equity(self) -> pl.DataFrame: ...


def _fail(table: str, detail: str) -> OutputOutOfRangeError:
    return OutputOutOfRangeError(f"output_out_of_range: {table}: {detail}")


def _check_schema(table: str, frame: pl.DataFrame, schema: pl.Schema) -> None:
    if frame.columns != schema.names():
        raise _fail(table, f"columnas {frame.columns} != {schema.names()}")
    for name, dtype in schema.items():
        actual = frame.schema[name]
        if actual != dtype:
            raise _fail(table, f"{name}: dtype {actual} != {dtype}")


def _check_values(table: str, frame: pl.DataFrame, schema: pl.Schema) -> None:
    for name, dtype in schema.items():
        column = frame.get_column(name)
        if column.null_count():
            raise _fail(table, f"{name}: contiene nulos")
        if dtype == pl.Float32 and not column.is_finite().all():
            raise _fail(table, f"{name}: contiene valores no finitos (NaN/inf)")


def _check_domain(table: str, column: pl.Series, domain: frozenset[int]) -> None:
    outside = sorted(set(column.unique().to_list()) - domain)
    if outside:
        raise _fail(table, f"{column.name}: valores {outside} fuera del dominio {sorted(domain)}")


def _check_sorted(table: str, column: pl.Series, *, strict: bool) -> None:
    steps = column.diff().drop_nulls()
    ok = bool((steps > 0).all()) if strict else bool((steps >= 0).all())
    if not ok:
        raise _fail(table, f"{column.name}: filas desordenadas")


def validate_engine_output(output: EngineTables, n_bars: int) -> None:
    """Comprueba que ``trades``/``equity`` cumplen RF-6 (plan §1.3).

    Columnas exactas y en orden, dtypes canónicos, sin nulos, magnitudes finitas,
    ``side ∈ {1}``, ``exit_reason ∈ {0, 1}``, ``exit_time_ms ≥ entry_time_ms``,
    ``drawdown ≤ 0``, ``trades`` ordenada por ``entry_time_ms``, ``equity`` estrictamente
    creciente en ``open_time_ms`` y ``equity.height == n_bars``.

    Raises:
        OutputOutOfRangeError: SI cualquier comprobación falla.
    """
    trades, equity = output.trades, output.equity

    _check_schema("trades", trades, TRADES_SCHEMA)
    _check_values("trades", trades, TRADES_SCHEMA)
    _check_domain("trades", trades.get_column("side"), SIDE_DOMAIN)
    _check_domain("trades", trades.get_column("exit_reason"), EXIT_REASON_DOMAIN)
    _check_sorted("trades", trades.get_column("entry_time_ms"), strict=False)
    if (trades.get_column("exit_time_ms") < trades.get_column("entry_time_ms")).any():
        raise _fail("trades", "exit_time_ms anterior a entry_time_ms")

    _check_schema("equity", equity, EQUITY_SCHEMA)
    if equity.height != n_bars:
        raise _fail("equity", f"{equity.height} filas != n_bars {n_bars}")
    _check_values("equity", equity, EQUITY_SCHEMA)
    _check_sorted("equity", equity.get_column("open_time_ms"), strict=True)
    if (equity.get_column("drawdown") > 0).any():
        raise _fail("equity", "drawdown positivo (debe ser ≤ 0)")
