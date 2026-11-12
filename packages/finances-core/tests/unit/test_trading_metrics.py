"""M1-M3: catálogo de métricas de trading (finances_core.metrics.trading).

Casos calculados a mano sobre el contrato canónico trades+equity, degenerados y registro de
objetivos.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import polars as pl
import pytest

from finances_core.backtest.metrics import compute as compute_run_summary
from finances_core.backtest.metrics import periods_per_year
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


def test_ledger_all_losses() -> None:
    # M1.1 (caso "sin ganancias"): ganancia bruta 0 -> profit_factor 0; sin ganadoras -> ratio None
    m = compute_ledger_metrics(
        make_trades([-10, -20, -30], [-0.01, -0.02, -0.03]), initial_cash=1000.0
    )
    assert m.net_profit == pytest.approx(-60.0)
    assert m.win_rate == 0.0
    assert m.profit_factor == pytest.approx(0.0)  # ganancia bruta 0 / pérdida > 0
    assert m.expectancy == pytest.approx(-20.0)
    assert m.avg_win_loss_ratio is None  # no hay ganadoras


def test_z_score_none_below_two_observations() -> None:
    # M1.2: una sola operación -> z_score None (N<2 y una sola clase); trade_count exacto
    m = compute_ledger_metrics(make_trades([10], [0.01]), initial_cash=1000.0)
    assert m.z_score is None
    assert m.trade_count == 1
    only_losses = compute_ledger_metrics(make_trades([-5, -6], [-0.01, -0.02]), initial_cash=1000.0)
    assert only_losses.z_score is None  # una sola clase (sin ganadoras)


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


def test_equity_metrics_match_backtest_run_summary() -> None:
    # M2.1: sharpe/sortino/max_drawdown_pct del catálogo neutral coinciden con los de
    # backtest.metrics.compute (RunSummary) para el mismo periods_per_year (misma fórmula tras
    # la promoción; guarda anti-deriva). `1h` -> periods_per_year("1h").
    trades = make_trades([100, -50, 30, -20, 10], [0.1, -0.05, 0.03, -0.02, 0.01])
    eq = make_equity(
        [1100, 1050, 1200, 900, 1000],
        [0.0, 1050 / 1100 - 1, 0.0, 900 / 1200 - 1, 1000 / 1200 - 1],
    )
    m = compute_equity_metrics(eq, initial_cash=1000.0, periods_per_year=periods_per_year("1h"))
    rs = compute_run_summary(trades, eq, initial_cash=1000.0, timeframe="1h")
    assert rs.sharpe is not None and rs.sortino is not None
    assert m.sharpe == pytest.approx(rs.sharpe, rel=1e-5)
    assert m.sortino == pytest.approx(rs.sortino, rel=1e-5)
    assert m.max_drawdown_pct == pytest.approx(rs.max_drawdown, rel=1e-5)


def test_ulcer_index_matches_higher_precision_reference() -> None:
    # M3.3 (precisión): la acumulación del ulcer index (RMS de drawdowns) difiere <1e-3 relativo
    # de una referencia de mayor precisión con math.fsum.
    drawdowns = [0.0, -0.031, -0.074, -0.250, -0.123, -0.045, -0.011, -0.198]
    eq = make_equity([1000.0] * len(drawdowns), drawdowns)
    m = compute_equity_metrics(eq, initial_cash=1000.0, periods_per_year=PPY)
    reference = math.sqrt(math.fsum(d * d for d in drawdowns) / len(drawdowns))
    assert m.ulcer_index == pytest.approx(reference, rel=1e-3)


def test_equity_ratios_hand_computed() -> None:
    # M2.2: martin_ratio, calmar, recovery_factor, annualized_recovery_factor a mano.
    # equity 1000->1200->900->1500 (n=4); con periods_per_year=n el exponente de anualización es 1.
    #   drawdown = [0, 0, -0.25, 0]; max_dd_pct=-0.25; max_dd_abs=-300; ulcer=sqrt(0.0625/4)=0.125
    #   growth=1.5 -> cagr=0.5 (exp 1); total_return=0.5; net_profit=500
    #   calmar=0.5/0.25=2 ; martin=0.5/0.125=4 ; recovery=500/300 ; ann_rec=(0.5*1)/0.25=2
    eq = make_equity([1000, 1200, 900, 1500], [0.0, 0.0, -0.25, 0.0])
    m = compute_equity_metrics(eq, initial_cash=1000.0, periods_per_year=4.0)
    assert m.calmar == pytest.approx(2.0, rel=1e-5)
    assert m.martin_ratio == pytest.approx(4.0, rel=1e-5)
    assert m.recovery_factor == pytest.approx(500 / 300, rel=1e-5)
    assert m.annualized_recovery_factor == pytest.approx(2.0, rel=1e-5)


def test_equity_degenerates_return_none() -> None:
    # M2.2 (degenerados): sin drawdown -> calmar/recovery/martin/ann_rec None;
    # 1 barra o equity plana -> sharpe/sortino/r_squared None.
    rising = make_equity([1000, 1100, 1200], [0.0, 0.0, 0.0])
    m = compute_equity_metrics(rising, initial_cash=1000.0, periods_per_year=3.0)
    assert m.calmar is None and m.recovery_factor is None
    assert m.martin_ratio is None and m.annualized_recovery_factor is None

    one_bar = compute_equity_metrics(
        make_equity([1000], [0.0]), initial_cash=1000.0, periods_per_year=1.0
    )
    assert one_bar.sharpe is None and one_bar.sortino is None and one_bar.r_squared is None

    flat = compute_equity_metrics(
        make_equity([1000, 1000, 1000], [0.0, 0.0, 0.0]), initial_cash=1000.0, periods_per_year=3.0
    )
    assert flat.sharpe is None  # varianza 0
    assert flat.r_squared is None  # varianza de equity 0


@pytest.mark.parametrize("name", sorted(OBJECTIVES))
def test_objective_value_defined_for_every_objective(name: str) -> None:
    # M3.2: para cada objetivo del registro, objective_value devuelve un float (finito o ±inf).
    trades = make_trades([100, -50, 30, -20, 10], [0.1, -0.05, 0.03, -0.02, 0.01])
    eq = make_equity(
        [1100, 1050, 1200, 900, 1000],
        [0.0, 1050 / 1100 - 1, 0.0, 900 / 1200 - 1, 1000 / 1200 - 1],
    )
    m = compute_trading_metrics(trades, eq, initial_cash=1000.0, periods_per_year=PPY)
    value = objective_value(name, m)
    assert isinstance(value, float)
    direction = OBJECTIVES[name].direction
    # degenerado coherente con la dirección: MAX -> -inf, MIN -> +inf (nunca al revés)
    assert value != (float("inf") if direction is Direction.MAX else float("-inf"))


def test_trading_metrics_are_deterministic() -> None:
    # M3.3 (determinismo): dos cálculos idénticos dan el mismo resultado campo a campo.
    trades = make_trades([100, -50, 30, -20, 10], [0.1, -0.05, 0.03, -0.02, 0.01])
    eq = make_equity(
        [1100, 1050, 1200, 900, 1000],
        [0.0, 1050 / 1100 - 1, 0.0, 900 / 1200 - 1, 1000 / 1200 - 1],
    )
    first = compute_trading_metrics(trades, eq, initial_cash=1000.0, periods_per_year=PPY)
    second = compute_trading_metrics(trades, eq, initial_cash=1000.0, periods_per_year=PPY)
    assert first == second


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
