"""Ayudas compartidas de las estrategias en Polars ``Float32`` (RF-2, plan §1.1, ADR-B12).

Cada ayuda recibe y devuelve una ``pl.Expr``: se componen dentro de ``candles.select``
sin materializar series intermedias. Solo usan datos de las barras ``<= t`` (RF-20).

Los cruces devuelven ``false`` (nunca nulo) mientras falta algún dato: el calentamiento
no genera señal (RF-2). El código de ``_lib`` no entra en el ``strategy_code_hash``
de cada estrategia; un cambio de su semántica exige subir ``CATALOG_VERSION`` (A22).

Importación (integradas y locales)::

    from finances_core.backtest.strategies._lib import cross_above, sma, position_from_events
"""

from __future__ import annotations

import polars as pl

Operand = pl.Expr | float


def _operands(b: Operand) -> tuple[pl.Expr, pl.Expr]:
    """``(b[t], b[t-1])``; un umbral constante vale lo mismo en ambas barras."""
    if isinstance(b, pl.Expr):
        return b, b.shift(1)
    threshold = pl.lit(b, dtype=pl.Float32)
    return threshold, threshold


def sma(x: pl.Expr, n: int) -> pl.Expr:
    """Media aritmética de ``x[t-n+1..t]``; nula para ``t < n-1``."""
    return x.rolling_mean(window_size=n)


def cross_above(a: pl.Expr, b: Operand) -> pl.Expr:
    """``a[t-1] <= b[t-1]`` **y** ``a[t] > b[t]`` (cruce estricto al alza, RF-2).

    Un empate en ``t`` no genera señal; un empate en ``t-1`` seguido de ``a > b`` sí.
    ``false`` donde falta ``a`` o ``b`` en ``t`` o ``t-1``.
    """
    now, prev = _operands(b)
    return ((a.shift(1) <= prev) & (a > now)).fill_null(value=False)


def cross_below(a: pl.Expr, b: Operand) -> pl.Expr:
    """``a[t-1] >= b[t-1]`` **y** ``a[t] < b[t]`` (cruce estricto a la baja, RF-2)."""
    now, prev = _operands(b)
    return ((a.shift(1) >= prev) & (a < now)).fill_null(value=False)


def _wilder_mean(values: pl.Expr, period: int) -> pl.Expr:
    """Media de Wilder: semilla SMA de ``values[1..period]`` en ``t = period`` y después
    ``media[t] = (media[t-1]·(period-1) + values[t]) / period``; nula para ``t < period``.

    ``ewm_mean(alpha=1/period, adjust=False)`` es exactamente esa recursión y arranca en
    el primer valor no nulo, que aquí es la semilla.
    """
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    seeded = (
        pl.when(t < period)
        .then(None)
        .when(t == period)
        .then(values.rolling_mean(window_size=period))
        .otherwise(values)
    )
    return seeded.ewm_mean(alpha=1 / period, adjust=False)


def rsi_wilder(close: pl.Expr, period: int) -> pl.Expr:
    """RSI de Wilder con semilla SMA (RF-2), en ``[0, 100]``; nulo para ``t < period``.

    ``Δ[t] = close[t] - close[t-1]``; ganancia ``max(Δ, 0)`` y pérdida ``max(-Δ, 0)``;
    ``RSI = 100 - 100 / (1 + RS)`` con ``RS = media_ganancia / media_pérdida``. SI la
    media de pérdidas es 0 y de ganancias > 0, ENTONCES 100; SI ambas son 0, 50.
    """
    delta = close - close.shift(1)
    gain = _wilder_mean(delta.clip(lower_bound=0.0), period)
    loss = _wilder_mean((-delta).clip(lower_bound=0.0), period)
    hundred = pl.lit(100.0, dtype=pl.Float32)
    rsi = (
        pl.when(loss == 0)
        .then(pl.when(gain > 0).then(hundred).otherwise(pl.lit(50.0, dtype=pl.Float32)))
        .otherwise(hundred - hundred / (1 + gain / loss))
    )
    return rsi.cast(pl.Float32)


def rolling_max_prev(x: pl.Expr, window: int) -> pl.Expr:
    """``max(x[t-window..t-1])``: excluye la barra actual; nulo para ``t < window``."""
    return x.shift(1).rolling_max(window_size=window)


def rolling_min_prev(x: pl.Expr, window: int) -> pl.Expr:
    """``min(x[t-window..t-1])``: excluye la barra actual; nulo para ``t < window``."""
    return x.shift(1).rolling_min(window_size=window)


def position_from_events(enter: pl.Expr, exit_: pl.Expr) -> pl.Expr:
    """Deriva una posición sostenida largo/plano a partir de eventos de entrada/salida.

    ``enter` tiene prioridad sobre ``exit_`` en el mismo ``t``.
    Sigue la semántica: ``when(enter).then(1).when(exit_).then(0).otherwise(None).forward_fill().fill_null(0)``.
    Cast a ``Int8``.
    """
    return (
        pl.when(enter)
        .then(1)
        .when(exit_)
        .then(0)
        .otherwise(None)
        .forward_fill()
        .fill_null(0)
        .cast(pl.Int8)
    )


__all__ = [
    "cross_above",
    "cross_below",
    "rolling_max_prev",
    "rolling_min_prev",
    "rsi_wilder",
    "sma",
    "position_from_events",
]
