"""G5: reader de evaluación honesta sobre una corrida guardada (backtest.evaluation).

``evaluate_run`` resuelve la corrida con ``results_store.find_run``, lee ``trades.parquet`` y el
``sl_frac`` del manifiesto (``request.risk``) y delega en ``metrics.compute_evaluation``. Read-only.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from finances_core.backtest import metrics, results_store
from finances_core.backtest.evaluation import evaluate_run
from finances_core.backtest.identity import run_id as compute_run_id
from finances_core.metrics import frames
from finances_core.metrics.evaluation import compute_evaluation
from finances_core.schemas.backtest import (
    BacktestRequest,
    ResolvedStrategy,
    RiskModel,
    RunManifest,
)

HOUR_MS = 3_600_000
START_MS = 1_700_000_000_000
STRATEGY_ID = "01890a5d-ac96-774b-bcce-b302099a8057"
DATASET = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}


def _trades(pnls: list[float]) -> pl.DataFrame:
    n = len(pnls)
    entries = [START_MS + 2 * i * HOUR_MS for i in range(n)]
    return pl.DataFrame(
        {
            "entry_time_ms": entries,
            "exit_time_ms": [t + HOUR_MS for t in entries],
            "side": [1] * n,
            "entry_price": [100.0] * n,
            "exit_price": [101.0] * n,
            "size": [1.0] * n,
            "fees": [0.0] * n,
            "pnl": list(pnls),
            "return": [0.0] * n,
            "exit_reason": [0] * n,
        }
    ).cast(dict(frames.TRADES_SCHEMA))


def _write_run(
    results: Path, *, pnls: list[float], risk: RiskModel | None
) -> tuple[str, pl.DataFrame]:
    body: dict[str, object] = {"dataset": DATASET, "strategy_id": STRATEGY_ID}
    if risk is not None:
        body["risk"] = risk.model_dump()
    request = BacktestRequest.model_validate(body)
    strategy = ResolvedStrategy(
        strategy_id=STRATEGY_ID,
        name="alpha",
        version=1,
        hash="a" * 64,
        kind="sma_cross",
        params={"fast": 2, "slow": 4},
        kind_version="1.0.0",
        code_hash="c" * 64,
        origin="local",
    )
    n_bars = 2 * len(pnls) + 2
    equity = pl.DataFrame(
        {
            "open_time_ms": [START_MS + i * HOUR_MS for i in range(n_bars)],
            "equity": [10_000.0] * n_bars,
            "drawdown": [0.0] * n_bars,
        },
        schema={"open_time_ms": pl.Int64, "equity": pl.Float32, "drawdown": pl.Float32},
    )
    trades = _trades(pnls)
    end_ms = START_MS + (n_bars - 1) * HOUR_MS
    rid = compute_run_id(
        request,
        strategy,
        start_ms=START_MS,
        end_ms=end_ms,
        data_hash="d" * 64,
        engine="vectorbt",
        engine_version="1.2.0",
    )
    manifest = RunManifest(
        run_id=rid,
        engine="vectorbt",
        engine_version="1.2.0",
        catalog_version="1.0.0",
        result_schema_version="1",
        request=request,
        strategy=strategy,
        start_ms=START_MS,
        end_ms=end_ms,
        n_bars=n_bars,
        data_hash="d" * 64,
        data_gaps=0,
        created_at_ms=1_800_000_000_000,
        duration_ms=1,
        warnings=[],
        python_version="3.12.0",
        libraries={"polars": "1.0.0"},
    )
    summary = metrics.compute(trades, equity, initial_cash=10_000.0, timeframe="1h")
    results_store.write_run(
        results, manifest=manifest, summary=summary, trades=trades, equity=equity
    )
    return rid, trades


def test_evaluate_run_matches_compute_on_stored_run(tmp_path: Path) -> None:
    pnls = [100.0, -50.0, 30.0, -20.0, 10.0]
    rid, trades = _write_run(tmp_path, pnls=pnls, risk=RiskModel(sl_frac=0.1))
    report = evaluate_run(tmp_path, rid, seed=7)
    assert report is not None
    # El reader extrae sl_frac del manifiesto y aplica el mismo cálculo puro.
    assert report == compute_evaluation(trades, sl_frac=0.1, seed=7)
    assert report.sl_frac == 0.1
    assert report.expectancy_r is not None


def test_evaluate_run_without_risk_has_no_r_metrics(tmp_path: Path) -> None:
    rid, _ = _write_run(tmp_path, pnls=[10.0, -5.0, 8.0], risk=None)
    report = evaluate_run(tmp_path, rid)
    assert report is not None
    assert report.sl_frac is None
    assert report.expectancy_r is None
    assert report.has_edge is False
    assert report.expectancy_cash is not None


def test_evaluate_run_not_found_returns_none(tmp_path: Path) -> None:
    assert evaluate_run(tmp_path, "f" * 64) is None
