"""Catálogo de métricas de trading, puro sobre el contrato ``trades``/``equity`` neutral.

Dos niveles (RF-3):

- **ledger** (solo ``trades``): :func:`compute_ledger_metrics`.
- **serie temporal** (``equity``): :func:`compute_equity_metrics` (anualización por
  ``periods_per_year``, RF-4).

:func:`compute_trading_metrics` los combina (``equity=None`` ⇒ solo ledger).

Convenciones (como :mod:`finances_core.backtest.metrics`): tasa libre de riesgo 0, cálculo con
expresiones Polars, extracción de escalares con ``.row(0, named=True)``. Una métrica indefinida
(sin operaciones, denominador 0, no finita) es ``None``; el **valor degenerado** para
optimización lo aplica :func:`objective_value`. El drawdown se almacena **con signo** (≤ 0),
igual que ``RunSummary.max_drawdown``; como objetivo se **maximiza** (acercarlo a 0 minimiza la
caída).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict

__all__ = [
    "Direction",
    "EquityMetrics",
    "LedgerMetrics",
    "OBJECTIVES",
    "Objective",
    "TradingMetrics",
    "compute_equity_metrics",
    "compute_ledger_metrics",
    "compute_trading_metrics",
    "objective_value",
]

#: Mínimo de observaciones para una desviación muestral (ddof=1).
_MIN_OBS = 2


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _ratio(num: float, den: float) -> float | None:
    return _finite(num / den) if den != 0 else None


def _opt_float(value: Any) -> float | None:  # noqa: ANN401 - escalar Polars (Any)
    return None if value is None else float(value)


# --------------------------------------------------------------------------- modelos


class LedgerMetrics(BaseModel):
    """Métricas que solo requieren el ledger de operaciones."""

    model_config = ConfigDict(extra="forbid")

    net_profit: float
    profit_factor: float | None
    win_rate: float | None
    expectancy: float | None
    avg_win_loss_ratio: float | None
    sqn: float | None
    z_score: float | None  # diagnóstico
    trade_count: int  # diagnóstico / constraint


class EquityMetrics(BaseModel):
    """Métricas que requieren la curva de equity."""

    model_config = ConfigDict(extra="forbid")

    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float  # ≤ 0
    max_drawdown_abs: float  # ≤ 0
    avg_drawdown: float | None  # ≤ 0
    max_drawdown_duration_ms: int
    calmar: float | None
    recovery_factor: float | None
    ulcer_index: float | None  # ≥ 0
    martin_ratio: float | None
    r_squared: float | None
    annualized_recovery_factor: float | None


class TradingMetrics(BaseModel):
    """Catálogo combinado; los campos de equity son ``None`` si no se pasó la curva."""

    model_config = ConfigDict(extra="forbid")

    # ledger
    net_profit: float
    profit_factor: float | None
    win_rate: float | None
    expectancy: float | None
    avg_win_loss_ratio: float | None
    sqn: float | None
    z_score: float | None
    trade_count: int
    # equity (None si equity no provista)
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown_pct: float | None = None
    max_drawdown_abs: float | None = None
    avg_drawdown: float | None = None
    max_drawdown_duration_ms: int | None = None
    calmar: float | None = None
    recovery_factor: float | None = None
    ulcer_index: float | None = None
    martin_ratio: float | None = None
    r_squared: float | None = None
    annualized_recovery_factor: float | None = None


# --------------------------------------------------------------------------- ledger


def _z_score(wins: int, losses: int, runs: int) -> float | None:
    """Z de la prueba de rachas (Wald–Wolfowitz) sobre la secuencia gana/pierde."""
    n = wins + losses
    if wins == 0 or losses == 0 or n < _MIN_OBS:
        return None
    expected = 2.0 * wins * losses / n + 1.0
    var = 2.0 * wins * losses * (2.0 * wins * losses - n) / (n * n * (n - 1))
    if var <= 0:
        return None
    return _finite((runs - expected) / math.sqrt(var))


def compute_ledger_metrics(trades: pl.DataFrame, *, initial_cash: float) -> LedgerMetrics:
    """Métricas de ledger desde ``trades`` canónicos (RF-3)."""
    n = trades.height
    if n == 0:
        return LedgerMetrics(
            net_profit=0.0,
            profit_factor=None,
            win_rate=None,
            expectancy=None,
            avg_win_loss_ratio=None,
            sqn=None,
            z_score=None,
            trade_count=0,
        )
    pnl = pl.col("pnl")
    row: dict[str, Any] = trades.select(
        net_profit=pnl.sum(),
        gross_profit=pnl.filter(pnl > 0).sum(),
        gross_loss=pnl.filter(pnl < 0).sum(),  # ≤ 0
        wins=(pnl > 0).sum(),
        losses=(pnl < 0).sum(),
        avg_win=pnl.filter(pnl > 0).mean(),
        avg_loss=pnl.filter(pnl < 0).mean(),  # ≤ 0
        ret_mean=pl.col("return").mean(),
        ret_std=pl.col("return").std(ddof=1),
        transitions=(pnl > 0).cast(pl.Int8).diff().drop_nulls().ne(0).sum(),
    ).row(0, named=True)

    net_profit = float(row["net_profit"])
    gross_loss = -float(row["gross_loss"])
    wins = int(row["wins"])
    losses = int(row["losses"])
    avg_win = _opt_float(row["avg_win"])
    avg_loss = _opt_float(row["avg_loss"])
    ret_std = _opt_float(row["ret_std"])
    runs = 1 + int(row["transitions"])

    return LedgerMetrics(
        net_profit=net_profit,
        profit_factor=_ratio(float(row["gross_profit"]), gross_loss),
        win_rate=wins / n,
        expectancy=net_profit / n,
        avg_win_loss_ratio=(
            _ratio(avg_win, -avg_loss) if avg_win is not None and avg_loss is not None else None
        ),
        sqn=(
            _finite(math.sqrt(n) * float(row["ret_mean"]) / ret_std)
            if ret_std is not None and ret_std > 0
            else None
        ),
        z_score=_z_score(wins, losses, runs),
        trade_count=n,
    )


# --------------------------------------------------------------------------- equity


def compute_equity_metrics(
    equity: pl.DataFrame, *, initial_cash: float, periods_per_year: float
) -> EquityMetrics:
    """Métricas de serie temporal desde la curva de ``equity`` (RF-3, RF-4).

    Raises:
        ValueError: SI ``equity`` no tiene barras.
    """
    n_bars = equity.height
    if n_bars == 0:
        msg = "compute_equity_metrics: equity sin barras"
        raise ValueError(msg)

    eqc = pl.col("equity")
    ddc = pl.col("drawdown")
    tc = pl.col("open_time_ms")
    peak = eqc.cum_max()
    r = eqc / eqc.shift(1).fill_null(initial_cash) - 1.0
    high_time = tc.filter(eqc >= peak)

    row: dict[str, Any] = equity.select(
        last=eqc.last(),
        r_mean=r.mean(),
        r_std=r.std(ddof=1),
        downside=r.clip(upper_bound=0.0).pow(2).mean().sqrt(),
        max_dd_pct=ddc.min(),
        max_dd_abs=(eqc - peak).min(),
        avg_dd=ddc.filter(ddc < 0).mean(),
        ulcer=ddc.pow(2).mean().sqrt(),
        eq_var=eqc.var(ddof=1),
        corr=pl.corr(pl.int_range(0, pl.len()).cast(pl.Float64), eqc),
        max_gap=high_time.diff().max(),
        last_high=high_time.last(),
        last_time=tc.last(),
        ends_high=eqc.last() >= peak.last(),
    ).row(0, named=True)

    last = float(row["last"])
    growth = last / initial_cash
    cagr_f = _finite(growth ** (periods_per_year / n_bars) - 1.0) if last > 0 else -1.0
    total_return = growth - 1.0
    sqrt_p = math.sqrt(periods_per_year)

    max_dd_pct = float(row["max_dd_pct"])
    max_dd_abs = float(row["max_dd_abs"])
    ulcer = float(row["ulcer"])
    net_profit = last - initial_cash
    r_mean = float(row["r_mean"])
    r_std = _opt_float(row["r_std"])
    downside = _opt_float(row["downside"])

    max_gap = int(row["max_gap"]) if row["max_gap"] is not None else 0
    tail = (
        0
        if bool(row["ends_high"]) or row["last_high"] is None
        else int(row["last_time"]) - int(row["last_high"])
    )
    duration = max(max_gap, tail, 0)

    corr = _opt_float(row["corr"])
    eq_var = _opt_float(row["eq_var"])
    r_squared = (
        _finite(corr**2)
        if corr is not None and eq_var is not None and eq_var > 0 and n_bars >= _MIN_OBS
        else None
    )

    return EquityMetrics(
        sharpe=(_finite(r_mean / r_std * sqrt_p) if r_std is not None and r_std > 0 else None),
        sortino=(
            _finite(r_mean / downside * sqrt_p) if downside is not None and downside > 0 else None
        ),
        max_drawdown_pct=max_dd_pct,
        max_drawdown_abs=max_dd_abs,
        avg_drawdown=_opt_float(row["avg_dd"]),
        max_drawdown_duration_ms=duration,
        calmar=(_ratio(cagr_f, abs(max_dd_pct)) if cagr_f is not None and max_dd_pct < 0 else None),
        recovery_factor=(_ratio(net_profit, abs(max_dd_abs)) if max_dd_abs < 0 else None),
        ulcer_index=ulcer,
        martin_ratio=(_ratio(cagr_f, ulcer) if cagr_f is not None and ulcer > 0 else None),
        r_squared=r_squared,
        annualized_recovery_factor=(
            _ratio(total_return * periods_per_year / n_bars, abs(max_dd_pct))
            if max_dd_pct < 0
            else None
        ),
    )


# --------------------------------------------------------------------------- combinada


def compute_trading_metrics(
    trades: pl.DataFrame,
    equity: pl.DataFrame | None,
    *,
    initial_cash: float,
    periods_per_year: float,
) -> TradingMetrics:
    """Combina ledger + (si hay ``equity``) serie temporal (RF-3)."""
    ledger = compute_ledger_metrics(trades, initial_cash=initial_cash)
    if equity is None:
        return TradingMetrics(**ledger.model_dump())
    eq = compute_equity_metrics(
        equity, initial_cash=initial_cash, periods_per_year=periods_per_year
    )
    return TradingMetrics(**ledger.model_dump(), **eq.model_dump())


# --------------------------------------------------------------------------- objetivos


class Direction(StrEnum):
    MAX = "max"
    MIN = "min"


class Objective(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    direction: Direction
    extract: Callable[[TradingMetrics], float | None]


def _obj(name: str, direction: Direction) -> Objective:
    return Objective(name=name, direction=direction, extract=lambda m: getattr(m, name))


#: Métricas optimizables (excluye los diagnósticos z_score y trade_count, RF-6).
OBJECTIVES: Mapping[str, Objective] = {
    o.name: o
    for o in (
        _obj("net_profit", Direction.MAX),
        _obj("profit_factor", Direction.MAX),
        _obj("win_rate", Direction.MAX),
        _obj("expectancy", Direction.MAX),
        _obj("avg_win_loss_ratio", Direction.MAX),
        _obj("sqn", Direction.MAX),
        _obj("sharpe", Direction.MAX),
        _obj("sortino", Direction.MAX),
        # drawdown con signo (≤ 0): maximizar acerca a 0 (menor caída)
        _obj("max_drawdown_pct", Direction.MAX),
        _obj("max_drawdown_abs", Direction.MAX),
        _obj("avg_drawdown", Direction.MAX),
        _obj("max_drawdown_duration_ms", Direction.MIN),
        _obj("calmar", Direction.MAX),
        _obj("recovery_factor", Direction.MAX),
        _obj("ulcer_index", Direction.MIN),
        _obj("martin_ratio", Direction.MAX),
        _obj("r_squared", Direction.MAX),
        _obj("annualized_recovery_factor", Direction.MAX),
    )
}


def objective_value(name: str, metrics: TradingMetrics) -> float:
    """Valor para optimización: ``None``/no finito → degenerado (−inf si MAX, +inf si MIN).

    Raises:
        KeyError: SI ``name`` no es un objetivo de :data:`OBJECTIVES`.
    """
    objective = OBJECTIVES[name]
    raw = objective.extract(metrics)
    value = _finite(float(raw)) if raw is not None else None
    if value is not None:
        return value
    return float("-inf") if objective.direction is Direction.MAX else float("inf")
