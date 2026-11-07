"""Slice 2: ft_vectorbt optimize (Optuna + walk-forward anclado).

Validación del sweep, folds anclados, estudio Optuna (determinismo, constraint, objetivo
desconocido) y el comando CLI. Sin red; reusa el *fixture* de contrato.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from finances_core.backtest.engine_api import CostModel
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import BacktestDataset
from typer.testing import CliRunner

from finances_backtester.ft_vectorbt import cli, optimize
from finances_backtester.ft_vectorbt.optimize import (
    InvalidSweepError,
    SweepAxis,
    SweepSpec,
    anchored_folds,
)

runner = CliRunner()
DATASET = BacktestDataset(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")
COSTS = CostModel(fees_bps=10.0, slippage_bps=5.0)


def _sweep() -> SweepSpec:
    return SweepSpec(
        axes=[
            SweepAxis(param="fast", type="int", low=5, high=15),
            SweepAxis(param="slow", type="int", low=20, high=60),
        ]
    )


# --------------------------------------------------------------------------- sweep / folds


def test_validate_sweep_ok_and_unknown(service_settings: FinancesSettings) -> None:
    optimize.validate_sweep(_sweep(), "sma_cross", service_settings)  # no lanza
    bad = SweepSpec(axes=[SweepAxis(param="no_existe", type="int", low=1, high=9)])
    with pytest.raises(InvalidSweepError):
        optimize.validate_sweep(bad, "sma_cross", service_settings)


def test_anchored_folds_hand_computed() -> None:
    assert anchored_folds(100, 2, 0.75) == [(76, 88), (88, 100)]
    with pytest.raises(ValueError, match="folds"):
        anchored_folds(100, 0, 0.75)
    with pytest.raises(ValueError, match="ratio"):
        anchored_folds(100, 2, 1.0)
    with pytest.raises(ValueError, match="insuficientes"):
        anchored_folds(10, 4, 0.9)


# --------------------------------------------------------------------------- estudio


def _run(settings: FinancesSettings, **kw: object) -> optimize.OptimizeResult:
    params: dict[str, object] = {
        "dataset": DATASET,
        "kind": "sma_cross",
        "sweep": _sweep(),
        "objective": "net_profit",
        "n_trials": 5,
        "folds": 2,
        "in_sample_ratio": 0.7,
        "min_trades": 1,
        "seed": 42,
        "costs": COSTS,
        "settings": settings,
    }
    params.update(kw)
    return optimize.optimize(**params)  # type: ignore[arg-type]


def test_optimize_runs_and_is_deterministic(service_settings: FinancesSettings) -> None:
    first = _run(service_settings)
    assert set(first.best_params) == {"fast", "slow"}
    assert first.n_trials == 5
    assert len(first.folds) == 2
    second = _run(service_settings)
    assert second.best_params == first.best_params  # misma semilla -> mismo resultado
    assert second.best_score == pytest.approx(first.best_score)


def test_unknown_objective_raises(service_settings: FinancesSettings) -> None:
    with pytest.raises(KeyError):
        _run(service_settings, objective="treynor")


def test_min_trades_constraint_prunes_all(service_settings: FinancesSettings) -> None:
    result = _run(service_settings, min_trades=10_000, objective="calmar")
    assert result.n_pruned == result.n_trials
    assert result.best_score == float("-inf")  # calmar es MAX -> degenerado


# --------------------------------------------------------------------------- CLI


def _write_sweep(tmp_path: Path) -> Path:
    path = tmp_path / "sweep.json"
    path.write_text(_sweep().model_dump_json(), encoding="utf-8")
    return path


def test_cli_optimize_runs(service_settings: FinancesSettings, tmp_path: Path) -> None:
    sweep = _write_sweep(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "--format",
            "json",
            "optimize",
            "-d",
            "binance/spot/BTCUSDT/1h",
            "--kind",
            "sma_cross",
            "--sweep",
            str(sweep),
            "--objective",
            "net_profit",
            "--n-trials",
            "5",
            "--folds",
            "2",
            "--in-sample-ratio",
            "0.7",
            "--min-trades",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "best_params" in result.output and "net_profit" in result.output


def test_cli_optimize_with_start_bounds_history(
    service_settings: FinancesSettings, tmp_path: Path
) -> None:
    sweep = _write_sweep(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "--format",
            "json",
            "optimize",
            "-d",
            "binance/spot/BTCUSDT/1h",
            "--kind",
            "sma_cross",
            "--sweep",
            str(sweep),
            "--n-trials",
            "3",
            "--folds",
            "2",
            "--min-trades",
            "1",
            "--start",
            "2024-01-01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "best_params" in result.output


def test_cli_optimize_unparseable_start_exit_2(
    service_settings: FinancesSettings, tmp_path: Path
) -> None:
    sweep = _write_sweep(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "optimize",
            "-d",
            "binance/spot/BTCUSDT/1h",
            "--kind",
            "sma_cross",
            "--sweep",
            str(sweep),
            "--start",
            "garbage",
        ],
    )
    assert result.exit_code == 2, result.output


def test_cli_unknown_objective_exit_2(service_settings: FinancesSettings, tmp_path: Path) -> None:
    sweep = _write_sweep(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "optimize",
            "-d",
            "binance/spot/BTCUSDT/1h",
            "--kind",
            "sma_cross",
            "--sweep",
            str(sweep),
            "--objective",
            "treynor",
        ],
    )
    assert result.exit_code == 2, result.output


def test_cli_invalid_sweep_exit_2(service_settings: FinancesSettings, tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        SweepSpec(axes=[SweepAxis(param="no_existe", type="int", low=1, high=9)]).model_dump_json(),
        encoding="utf-8",
    )
    result = runner.invoke(
        cli.app,
        ["optimize", "-d", "binance/spot/BTCUSDT/1h", "--kind", "sma_cross", "--sweep", str(path)],
    )
    assert result.exit_code == 2, result.output


def test_cli_requires_exactly_one_of_strategy_or_kind(
    service_settings: FinancesSettings, tmp_path: Path
) -> None:
    sweep = _write_sweep(tmp_path)
    result = runner.invoke(
        cli.app, ["optimize", "-d", "binance/spot/BTCUSDT/1h", "--sweep", str(sweep)]
    )
    assert result.exit_code == 2, result.output  # ni strategy-id ni kind
