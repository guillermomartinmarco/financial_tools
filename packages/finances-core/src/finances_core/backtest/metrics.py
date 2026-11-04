"""Métricas del resumen (RF-7, A8, A9, ADR-B03), idénticas para todos los motores.

Se calculan desde ``equity`` y ``trades`` canónicos (RF-6) con expresiones Polars sobre
``Float32`` (Art. 8): ``r[t] = equity[t]/equity[t-1] - 1`` con ``equity[-1] =
initial_cash``; media y desviación son agregados de Polars y el cociente final y ``√P`` se
aplican sobre el valor ya agregado. ``P = 365,25·86 400 000 / tf_ms`` (año de calendario
continuo, mercado 24/7, tasa libre de riesgo 0; A8). La precisión de las acumulaciones se
verifica contra una referencia de mayor precisión en ``tests/`` (RFC-0004, 1e-3).
"""

from __future__ import annotations

import math

import polars as pl

from finances_core.backtest.frames import EXIT_REASON_END_OF_DATA
from finances_core.marketdata.timeframes import DAY_MS, timeframe_duration_ms
from finances_core.schemas.backtest import RunSummary

#: Días del año de calendario continuo (A8).
DAYS_PER_YEAR = 365.25

_F32 = pl.Float32


def periods_per_year(timeframe: str) -> float:
    """``P = 365,25·86 400 000 / tf_ms`` (``1m`` → 525 960, ``1h`` → 8 766, ``1d`` → 365,25).

    Raises:
        ValueError: SI el timeframe no es uno de los soportados.
    """
    return DAYS_PER_YEAR * DAY_MS / timeframe_duration_ms(timeframe)


def _lit(value: float) -> pl.Expr:
    return pl.lit(value, dtype=_F32)


def _ratio_or_null(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return pl.when(denominator > 0).then(numerator / denominator).otherwise(None)


def _exposed_bars(trades: pl.DataFrame, open_times: pl.Series) -> int:
    """Barras con posición larga al cierre.

    Una salida por señal se ejecuta en la apertura de su barra, que ya cierra plana: cuenta
    ``[entry, exit)``. La liquidación final (``end_of_data``) se hace al cierre de la última
    barra con la posición todavía abierta en ella: cuenta ``[entry, exit]``.
    """
    if trades.is_empty():
        return 0
    first = open_times.search_sorted(trades.get_column("entry_time_ms"), side="left")
    last = open_times.search_sorted(trades.get_column("exit_time_ms"), side="left")
    inclusive = (trades.get_column("exit_reason") == EXIT_REASON_END_OF_DATA).cast(pl.Int64)
    return int((last.cast(pl.Int64) - first.cast(pl.Int64) + inclusive).sum())


def _optional(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _finite_or_null(value: object) -> float | None:
    """``None`` si el valor no es finito (deriva de RF-7: anualización desbordada)."""
    number = _optional(value)
    return number if number is not None and math.isfinite(number) else None


def compute(
    trades: pl.DataFrame, equity: pl.DataFrame, *, initial_cash: float, timeframe: str
) -> RunSummary:
    """Calcula :class:`RunSummary` (RF-7) desde las tablas canónicas ya validadas.

    ``sharpe`` es ``null`` con desviación 0 o menos de 2 barras; ``sortino`` con
    denominador 0; ``win_rate`` sin operaciones. ``cagr = -1`` si ``equity[último] ≤ 0``
    y ``null`` si la anualización no es finita (corridas muy cortas en timeframes chicos).

    Raises:
        ValueError: SI ``equity`` no tiene barras o el timeframe es desconocido.
    """
    n_bars = equity.height
    if n_bars == 0:
        msg = "metrics: equity sin barras (n_bars = 0)"
        raise ValueError(msg)
    periods = periods_per_year(timeframe)
    sqrt_p = _lit(periods**0.5)

    eq = pl.col("equity")
    r = eq / eq.shift(1).fill_null(_lit(initial_cash)) - 1
    growth = eq.last() / _lit(initial_cash)
    downside = r.clip(upper_bound=0).pow(2).mean().sqrt()
    stats = equity.select(
        final_equity=eq.last(),
        total_return=growth - 1,
        cagr=pl.when(eq.last() > 0)
        .then(growth.pow(_lit(periods / n_bars)) - 1)
        .otherwise(_lit(-1.0)),
        max_drawdown=pl.col("drawdown").min(),
        sharpe=_ratio_or_null(r.mean(), r.std(ddof=1).fill_null(0)) * sqrt_p,
        sortino=_ratio_or_null(r.mean(), downside) * sqrt_p,
    ).row(0, named=True)

    n_trades = trades.height
    win_rate = trades.select((pl.col("pnl") > 0).cast(_F32).mean()).item() if n_trades else None
    exposure = equity.select(
        _lit(_exposed_bars(trades, equity.get_column("open_time_ms"))) / _lit(n_bars)
    ).item()

    return RunSummary(
        total_return=float(stats["total_return"]),
        cagr=_finite_or_null(stats["cagr"]),
        max_drawdown=float(stats["max_drawdown"]),
        sharpe=_optional(stats["sharpe"]),
        sortino=_optional(stats["sortino"]),
        win_rate=_optional(win_rate),
        n_trades=n_trades,
        exposure=float(exposure),
        initial_cash=initial_cash,
        final_equity=float(stats["final_equity"]),
        n_bars=n_bars,
    )
