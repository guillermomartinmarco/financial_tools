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


def ema(x: pl.Expr, n: int) -> pl.Expr:
    """Media exponencial ``alpha = 2/(n+1)`` sembrada con ``SMA(n)`` en ``t = n-1`` (L1).

    ``ema[t] = alpha·x[t] + (1-alpha)·ema[t-1]`` para ``t >= n``; nula para ``t < n-1``.
    ``ewm_mean(adjust=False)`` es esa recursión y arranca en el primer valor no nulo (la
    semilla), así que es prefijo-estable.
    """
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    seeded = (
        pl.when(t < n - 1)
        .then(None)
        .when(t == n - 1)
        .then(x.rolling_mean(window_size=n))
        .otherwise(x)
    )
    return seeded.ewm_mean(alpha=2.0 / (n + 1.0), adjust=False).cast(pl.Float32)


def atr(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """Average True Range de Wilder (L1); nulo para ``t < n``.

    ``TR[t] = max(high-low, |high-close[t-1]|, |low-close[t-1]|)``; en ``t=0`` (sin
    ``close[t-1]``), ``TR[0] = high[0]-low[0]``. ``atr`` es la media de Wilder de ``TR``
    con periodo ``n`` (misma recursión sembrada que ``rsi_wilder``).
    """
    prev_close = close.shift(1)
    true_range = pl.max_horizontal(high - low, (high - prev_close).abs(), (low - prev_close).abs())
    return _wilder_mean(true_range, n).cast(pl.Float32)


def rolling_std_stable(x: pl.Expr, n: int) -> pl.Expr:
    """Desviación estándar **poblacional** (``ddof=0``) de ``x[t-n+1..t]``, prefijo-estable.

    ``var = E[x²] - E[x]²`` con sumas rodantes en ``Float64`` (**excepción RFC-0007**: la
    identidad sufre cancelación catastrófica en ``Float32`` —171% de error a escala BTC—,
    mismo motivo que RFC-0006 en ``ft_indicators``), recortada a ``>= 0`` y con raíz
    casteada a ``Float32``; nula para ``t < n-1``. Se reconstruye con ``rolling_sum`` porque
    ``rolling_std``/``rolling_var`` de Polars **no** son prefijo-estables (bug Bollinger #50).
    """
    x64 = x.cast(pl.Float64)
    mean = x64.rolling_sum(window_size=n) / n
    mean_sq = (x64 * x64).rolling_sum(window_size=n) / n
    variance = (mean_sq - mean * mean).clip(lower_bound=0.0)
    return variance.sqrt().cast(pl.Float32)


def zscore(x: pl.Expr, n: int) -> pl.Expr:
    """``(x[t] - SMA(x,n)[t]) / rolling_std_stable(x,n)[t]`` (L1); nulo si ``std == 0``.

    Cálculo interno en ``Float64`` (media y desviación poblacionales; **excepción RFC-0007**,
    como ``rolling_std_stable``); salida ``Float32``; nulo para ``t < n-1``.
    """
    x64 = x.cast(pl.Float64)
    mean = x64.rolling_sum(window_size=n) / n
    mean_sq = (x64 * x64).rolling_sum(window_size=n) / n
    std = (mean_sq - mean * mean).clip(lower_bound=0.0).sqrt()
    return pl.when(std > 0).then((x64 - mean) / std).otherwise(None).cast(pl.Float32)


def roc(x: pl.Expr, n: int) -> pl.Expr:
    """Tasa de cambio como **fracción**: ``x[t]/x[t-n] - 1`` (L1); nula si ``x[t-n] == 0``.

    Nula para ``t < n`` (no hay ``x[t-n]``).
    """
    prev = x.shift(n)
    return pl.when(prev != 0).then(x / prev - 1.0).otherwise(None).cast(pl.Float32)


def wma(x: pl.Expr, n: int) -> pl.Expr:
    """Media ponderada lineal: peso ``n`` a la barra actual, ``1`` a ``t-n+1`` (L1).

    ``Σ_{k=0}^{n-1} (n-k)·x[t-k] / (n(n+1)/2)``; nula para ``t < n-1``. Salida ``Float32``;
    sin cancelación (media ponderada), así que no necesita doble precisión. Solo mira el
    pasado, así que es prefijo-estable.
    """
    weighted = n * x
    for k in range(1, n):
        weighted = weighted + (n - k) * x.shift(k)
    return (weighted / (n * (n + 1) / 2.0)).cast(pl.Float32)


def obv(close: pl.Expr, volume: pl.Expr) -> pl.Expr:
    """On-Balance Volume acumulado (L1): ``obv[0] = 0``, prefijo-estable por ``cum_sum``.

    ``obv[t] = obv[t-1] + sign(close[t]-close[t-1])·volume[t]`` con ``sign in {-1,0,+1}``
    (``0`` en empate). Sin nulos.
    """
    delta = close - close.shift(1)
    step = pl.when(delta > 0).then(volume).when(delta < 0).then(-volume).otherwise(0.0)
    return step.cum_sum().cast(pl.Float32)


def vwap(high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr, n: int) -> pl.Expr:
    """VWAP de **ventana rodante** ``n`` (sin estado de sesión) (L1); nulo si ``Σ volume == 0``.

    ``tp = (high+low+close)/3``; ``vwap[t] = Σ(tp·volume)[t-n+1..t] / Σ(volume)[t-n+1..t]``;
    nulo para ``t < n-1``. Cociente de sumas positivas (sin cancelación), ``Float32``. Solo
    mira ``<= t``: prefijo-estable.
    """
    typical = (high + low + close) / 3.0
    num = (typical * volume).rolling_sum(window_size=n)
    den = volume.rolling_sum(window_size=n)
    return pl.when(den > 0).then(num / den).otherwise(None).cast(pl.Float32)


def rolling_max_prev(x: pl.Expr, window: int) -> pl.Expr:
    """``max(x[t-window..t-1])``: excluye la barra actual; nulo para ``t < window``."""
    return x.shift(1).rolling_max(window_size=window)


def rolling_min_prev(x: pl.Expr, window: int) -> pl.Expr:
    """``min(x[t-window..t-1])``: excluye la barra actual; nulo para ``t < window``."""
    return x.shift(1).rolling_min(window_size=window)


def position_from_events(enter: pl.Expr, exit_: pl.Expr) -> pl.Expr:
    """Deriva una posición sostenida largo/plano a partir de eventos de entrada/salida.

    ``enter`` tiene prioridad sobre ``exit_`` en el mismo ``t``. Sigue la semántica
    ``when(enter).then(1).when(exit_).then(0).otherwise(None)``, luego ``forward_fill()``
    y ``fill_null(0)``, con cast final a ``Int8``.
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
    "atr",
    "cross_above",
    "cross_below",
    "ema",
    "obv",
    "position_from_events",
    "roc",
    "rolling_max_prev",
    "rolling_min_prev",
    "rolling_std_stable",
    "rsi_wilder",
    "sma",
    "vwap",
    "wma",
    "zscore",
]
