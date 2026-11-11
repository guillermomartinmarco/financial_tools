"""Evaluación honesta de una corrida **guardada** (Enmienda G5): lee y delega en las métricas.

Puente read-only entre el almacén de resultados (:mod:`finances_core.backtest.results_store`) y
el cálculo puro (:func:`finances_core.metrics.evaluation.compute_evaluation`): resuelve la
corrida por ``run_id``, lee ``trades.parquet`` y toma el ``sl_frac`` del manifiesto
(``request.risk``, persistido desde la Enmienda G4), sin recomputar la simulación.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from finances_core.backtest import results_store
from finances_core.metrics.evaluation import (
    DEFAULT_CI_LEVEL,
    DEFAULT_MIN_TRADES,
    DEFAULT_N_RESAMPLES,
    DEFAULT_SEED,
    EvaluationReport,
    compute_evaluation,
)

__all__ = ["evaluate_run"]


def evaluate_run(  # noqa: PLR0913 - parámetros de configuración del reporte (keyword-only)
    results_dir: Path,
    run_id: str,
    *,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = DEFAULT_SEED,
    min_trades: int = DEFAULT_MIN_TRADES,
) -> EvaluationReport | None:
    """:class:`EvaluationReport` de la corrida ``run_id`` o ``None`` si no existe (RF-9).

    Read-only: lee ``trades.parquet`` y el ``sl_frac`` del manifiesto; no recomputa la
    simulación. Sin ``sl_frac`` (corrida sin stop) el reporte no trae métricas en R.
    """
    stored = results_store.find_run(results_dir, run_id)
    if stored is None:
        return None
    trades = pl.read_parquet(stored.path / results_store.TRADES_FILE)
    risk = stored.manifest.request.risk
    sl_frac = risk.sl_frac if risk is not None else None
    return compute_evaluation(
        trades,
        sl_frac=sl_frac,
        ci_level=ci_level,
        n_resamples=n_resamples,
        seed=seed,
        min_trades=min_trades,
    )
