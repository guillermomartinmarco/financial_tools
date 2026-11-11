"""G5: evaluación honesta — expectancy en R-múltiplos + bootstrap CI (metrics.evaluation).

Casos calculados a mano sobre el contrato ``trades`` canónico. R[t] = pnl / (entry_price ·
size · sl_frac); expectancy_r = media de R; el IC viene de un bootstrap IID sembrado
(determinista). "Edge" ⇔ r_ci_low > 0.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl
import pytest

from finances_core.metrics import frames
from finances_core.metrics.evaluation import (
    DEFAULT_CI_LEVEL,
    DEFAULT_MIN_TRADES,
    DEFAULT_N_RESAMPLES,
    DEFAULT_SEED,
    EvaluationReport,
    compute_evaluation,
)


def make_trades(
    pnls: Sequence[float],
    *,
    entry_prices: Sequence[float] | None = None,
    sizes: Sequence[float] | None = None,
) -> pl.DataFrame:
    n = len(pnls)
    entry_prices = list(entry_prices) if entry_prices is not None else [100.0] * n
    sizes = list(sizes) if sizes is not None else [1.0] * n
    return pl.DataFrame(
        {
            "entry_time_ms": pl.Series([i * 3_600_000 for i in range(n)], dtype=pl.Int64),
            "exit_time_ms": pl.Series([(i + 1) * 3_600_000 for i in range(n)], dtype=pl.Int64),
            "side": pl.Series([1] * n, dtype=pl.Int8),
            "entry_price": pl.Series(entry_prices, dtype=pl.Float32),
            "exit_price": pl.Series([101.0] * n, dtype=pl.Float32),
            "size": pl.Series(sizes, dtype=pl.Float32),
            "fees": pl.Series([0.0] * n, dtype=pl.Float32),
            "pnl": pl.Series(list(pnls), dtype=pl.Float32),
            "return": pl.Series([0.0] * n, dtype=pl.Float32),
            "exit_reason": pl.Series([0] * n, dtype=pl.Int8),
        }
    ).cast(dict(frames.TRADES_SCHEMA))


def test_expectancy_r_hand_computed() -> None:
    # entry_price=100, size=1, sl_frac=0.1 -> denom=10 -> R = pnl/10.
    # pnls [100,-50,30,-20,10] -> R [10,-5,3,-2,1] -> mean 1.4; cash expectancy 70/5=14.
    trades = make_trades([100, -50, 30, -20, 10])
    rep = compute_evaluation(trades, sl_frac=0.1)
    assert isinstance(rep, EvaluationReport)
    assert rep.n_trades == 5
    assert rep.sl_frac == pytest.approx(0.1)
    assert rep.expectancy_r == pytest.approx(1.4, rel=1e-4)
    assert rep.expectancy_cash == pytest.approx(14.0, rel=1e-4)


def test_r_uses_per_trade_denominator() -> None:
    # entry_price [100,200], size 1, pnl [10,10], sl_frac 0.1 -> denom [10,20] -> R [1.0,0.5]
    # -> expectancy_r 0.75.
    trades = make_trades([10, 10], entry_prices=[100.0, 200.0])
    rep = compute_evaluation(trades, sl_frac=0.1)
    assert rep.expectancy_r == pytest.approx(0.75, rel=1e-4)


def test_ci_brackets_expectancy_and_is_deterministic() -> None:
    trades = make_trades([100, -50, 30, -20, 10])
    a = compute_evaluation(trades, sl_frac=0.1, seed=7)
    b = compute_evaluation(trades, sl_frac=0.1, seed=7)
    # determinismo: misma semilla -> mismo IC.
    assert (a.r_ci_low, a.r_ci_high) == (b.r_ci_low, b.r_ci_high)
    assert a.r_ci_low is not None and a.r_ci_high is not None
    assert a.r_ci_low <= a.expectancy_r <= a.r_ci_high
    # otra semilla puede dar otro IC (no exigido, pero el método depende de la semilla).
    assert a.seed == 7


def test_edge_when_all_positive() -> None:
    # min_trades bajo para aislar la lógica del IC (el gate de muestra se prueba aparte).
    rep = compute_evaluation(make_trades([10, 8, 12, 9, 11, 10, 9]), sl_frac=0.1, min_trades=1)
    assert rep.expectancy_r is not None and rep.expectancy_r > 0
    assert rep.r_ci_low is not None and rep.r_ci_low > 0
    assert rep.sufficient_sample is True
    assert rep.has_edge is True


def test_no_edge_when_negative_mean() -> None:
    rep = compute_evaluation(make_trades([5, -10, 5, -10, 5, -10]), sl_frac=0.1, min_trades=1)
    assert rep.has_edge is False
    assert rep.r_ci_low is not None and rep.r_ci_low <= 0


def test_min_trades_gate_blocks_edge_on_small_sample() -> None:
    # 5 operaciones todas positivas: con min_trades=5 hay edge; con 6 no (muestra insuficiente),
    # aunque el IC siga siendo > 0. Anti-p-hacking: no se afirma edge con muy pocas operaciones.
    trades = make_trades([10, 8, 12, 9, 11])
    ok = compute_evaluation(trades, sl_frac=0.1, min_trades=5)
    assert ok.sufficient_sample is True and ok.has_edge is True
    blocked = compute_evaluation(trades, sl_frac=0.1, min_trades=6)
    assert blocked.sufficient_sample is False
    assert blocked.has_edge is False
    assert blocked.r_ci_low is not None and blocked.r_ci_low > 0  # el IC sí es positivo


def test_no_sl_frac_means_no_r_metrics() -> None:
    trades = make_trades([100, -50, 30])
    rep = compute_evaluation(trades, sl_frac=None)
    assert rep.sl_frac is None
    assert rep.expectancy_r is None
    assert rep.r_ci_low is None and rep.r_ci_high is None
    assert rep.has_edge is False
    # la expectancy en cash sigue disponible como referencia.
    assert rep.expectancy_cash == pytest.approx(80 / 3, rel=1e-4)


def test_empty_trades() -> None:
    rep = compute_evaluation(make_trades([]), sl_frac=0.1)
    assert rep.n_trades == 0
    assert rep.expectancy_r is None
    assert rep.expectancy_cash is None
    assert rep.r_ci_low is None and rep.r_ci_high is None
    assert rep.has_edge is False


def test_report_records_bootstrap_params() -> None:
    trades = make_trades([10, 8, 12])
    rep = compute_evaluation(
        trades, sl_frac=0.2, ci_level=0.9, n_resamples=500, seed=99, min_trades=3
    )
    assert (rep.ci_level, rep.n_resamples, rep.seed, rep.min_trades) == (0.9, 500, 99, 3)
    # defaults expuestos.
    assert (DEFAULT_CI_LEVEL, DEFAULT_N_RESAMPLES, DEFAULT_SEED, DEFAULT_MIN_TRADES) == (
        0.95,
        10_000,
        1234,
        30,
    )


def test_single_trade_does_not_claim_edge() -> None:
    # Un solo trade: el IC colapsa a R[0] (bootstrap degenerado). Con el default min_trades=30,
    # NO se afirma edge (anti-p-hacking): muestra insuficiente.
    trades = make_trades([20])  # R = 20/(100*1*0.1) = 2.0
    rep = compute_evaluation(trades, sl_frac=0.1)
    assert rep.expectancy_r == pytest.approx(2.0, rel=1e-4)
    assert rep.r_ci_low == pytest.approx(2.0, rel=1e-4)
    assert rep.r_ci_high == pytest.approx(2.0, rel=1e-4)
    assert rep.sufficient_sample is False
    assert rep.has_edge is False
