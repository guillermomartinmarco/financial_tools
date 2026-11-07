"""M1-M3: catálogo de métricas de trading (finances_core.metrics.trading).

Casos calculados a mano sobre el contrato canónico trades+equity, degenerados y registro de
objetivos.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import polars as pl
import pytest

from finances_core.metrics import frames
from finances_core.metrics.trading import (
    OBJECTIVES,
    Direction,
    compute_equity_metrics,
    compute_ledger_metrics,
    compute_trading_metrics,
    objective_value,
)

PPY = 8766.0  # periods_per_year de 1h (referencia)


def make_trades(pnls: Sequence[float], returns: Sequence[float]) -> pl.DataFrame:
    n = len(pnls)
    return pl.DataFrame(
        {
            "entry_time_ms": pl.Series([i * 3_600_000 for i in range(n)], dtype=pl.Int64),
            "exit_time_ms": pl.Series([(i + 1) * 3_600_000 for i in range(n)], dtype=pl.Int64),
            "side": pl.Series([1] * n, dtype=pl.Int8),
            "entry_price": pl.Series([100.0] * n, dtype=pl.Float32),
            "exit_price": pl.Series([101.0] * n, dtype=pl.Float32),
            "size": pl.Series([1.0] * n, dtype=pl.Float32),
            "fees": pl.Series([0.0] * n, dtype=pl.Float32),
            "pnl": pl.Series(list(pnls), dtype=pl.Float32),
            "return": pl.Series(list(returns), dtype=pl.Float32),
            "exit_reason": pl.Series([0] * n, dtype=pl.Int8),
        }
    ).cast(dict(frames.TRADES_SCHEMA))


def make_equity(equities: Sequence[float], drawdowns: Sequence[float]) -> pl.DataFrame:
    n = len(equities)
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series([i * 3_600_000 for i in range(n)], dtype=pl.Int64),
            "equity": pl.Series(list(equities), dtype=pl.Float32),
            "drawdown": pl.Series(list(drawdowns), dtype=pl.Float32),
        }
    ).cast(dict(frames.EQUITY_SCHEMA))


# --------------------------------------------------------------------------- ledger


def test_ledger_metrics_hand_computed() -> None:
    trades = make_trades([100, -50, 30, -20, 10], [0.1, -0.05, 0.03, -0.02, 0.01])
    m = compute_ledger_metrics(trades, initial_cash=1000.0)
    assert m.net_profit == pytest.approx(70.0)
    assert m.profit_factor == pytest.approx(140 / 70)  # 2.0
    assert m.win_rate == pytest.approx(0.6)
    assert m.expectancy == pytest.approx(14.0)
    assert m.avg_win_loss_ratio == pytest.approx((140 / 3) / 35, rel=1e-4)
    assert m.sqn == pytest.approx(0.5508, rel=1e-3)
    assert m.z_score == pytest.approx(1.7457, rel=1e-3)  # rachas W L W L W
    assert m.trade_count == 5


def test_ledger_degenerates() -> None:
    empty = compute_ledger_metrics(make_trades([], []), initial_cash=1000.0)
    assert empty.net_profit == 0.0
    assert empty.profit_factor is None and empty.trade_count == 0
    no_loss = compute_ledger_metrics(make_trades([10, 20], [0.01, 0.02]), initial_cash=1000.0)
    assert no_loss.profit_factor is None  # pérdida bruta 0 -> indefinido
    assert no_loss.avg_win_loss_ratio is None


# --------------------------------------------------------------------------- equity


def test_equity_metrics_hand_computed() -> None:
    eq = make_equity(
        [1100, 1050, 1200, 900, 1000],
        [0.0, 1050 / 1100 - 1, 0.0, 900 / 1200 - 1, 1000 / 1200 - 1],
    )
    m = compute_equity_metrics(eq, initial_cash=1000.0, periods_per_year=PPY)
    assert m.max_drawdown_pct == pytest.approx(-0.25, rel=1e-5)
    assert m.max_drawdown_abs == pytest.approx(-300.0, rel=1e-5)
    assert m.avg_drawdown == pytest.approx(-0.154040, rel=1e-3)
    assert m.max_drawdown_duration_ms == 7_200_000
    assert m.ulcer_index == pytest.approx(0.135893, rel=1e-3)
    assert m.recovery_factor == pytest.approx(0.0, abs=1e-6)  # net_profit 0
    assert m.calmar == pytest.approx(0.0, abs=1e-6)  # cagr 0 (equity vuelve a 1000)
    assert m.sharpe is not None and math.isfinite(m.sharpe)
    assert m.sortino is not None and math.isfinite(m.sortino)
    assert m.r_squared is not None and 0.0 <= m.r_squared <= 1.0


def test_r_squared_perfect_line() -> None:
    eq = make_equity([1000, 1100, 1200, 1300, 1400], [0.0, 0.0, 0.0, 0.0, 0.0])
    m = compute_equity_metrics(eq, initial_cash=1000.0, periods_per_year=PPY)
    assert m.r_squared == pytest.approx(1.0, abs=1e-6)  # recta perfecta


# --------------------------------------------------------------------------- combinada


def test_combined_ledger_only_leaves_equity_none() -> None:
    trades = make_trades([100, -50], [0.1, -0.05])
    m = compute_trading_metrics(trades, None, initial_cash=1000.0, periods_per_year=PPY)
    assert m.trade_count == 2 and m.net_profit == pytest.approx(50.0)
    assert m.sharpe is None and m.max_drawdown_pct is None and m.ulcer_index is None


# --------------------------------------------------------------------------- objetivos


def test_objectives_registry_excludes_diagnostics() -> None:
    assert "z_score" not in OBJECTIVES
    assert "trade_count" not in OBJECTIVES
    assert OBJECTIVES["sharpe"].direction is Direction.MAX
    assert OBJECTIVES["ulcer_index"].direction is Direction.MIN
    assert OBJECTIVES["max_drawdown_pct"].direction is Direction.MAX  # ≤0, maximizar hacia 0


def test_objective_value_degenerates_by_direction() -> None:
    trades = make_trades([10, 20], [0.01, 0.02])  # sin pérdidas, sin equity
    m = compute_trading_metrics(trades, None, initial_cash=1000.0, periods_per_year=PPY)
    assert objective_value("profit_factor", m) == float("-inf")  # None, MAX
    assert objective_value("max_drawdown_pct", m) == float("-inf")  # None, MAX
    assert objective_value("ulcer_index", m) == float("inf")  # None, MIN
    assert objective_value("max_drawdown_duration_ms", m) == float("inf")  # None, MIN
    assert objective_value("net_profit", m) == pytest.approx(30.0)


def test_objective_value_unknown_name_raises() -> None:
    trades = make_trades([10], [0.01])
    m = compute_trading_metrics(trades, None, initial_cash=1000.0, periods_per_year=PPY)
    with pytest.raises(KeyError):
        objective_value("treynor", m)
