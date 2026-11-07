"""Optimización de parámetros de una estrategia con Optuna (Slice 2, RF-1…RF-8).

TPE sembrado + walk-forward anclado (score sobre OOS) + objetivo del catálogo
``finances_core.metrics``. Evaluación **en memoria** (deriva): un solo ``engine.run`` por
trial sobre el histórico completo (warmup correcto) y métricas por **ventana OOS**; la clase
de la estrategia se usa vía :func:`finances_core.backtest.evaluate.evaluate_params` (el motor
no puede importar el catálogo, RF-21). No persiste cada trial (RF-7).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import optuna
import polars as pl
from finances_core.backtest import strategy_source
from finances_core.backtest.candles import read_candles
from finances_core.backtest.engine_api import CostModel, EngineOutput
from finances_core.backtest.evaluate import evaluate_params
from finances_core.backtest.metrics import periods_per_year
from finances_core.config import FinancesSettings
from finances_core.metrics import (
    OBJECTIVES,
    Direction,
    compute_trading_metrics,
    objective_value,
)
from finances_core.schemas.backtest import BacktestDataset
from pydantic import BaseModel, ConfigDict, model_validator

from finances_backtester.ft_vectorbt.engine import VectorbtEngine

optuna.logging.set_verbosity(optuna.logging.WARNING)

FoldAgg = Literal["mean", "min", "median"]


def _aggregate(values: Sequence[float], how: FoldAgg) -> float:
    if how == "min":
        return min(values)
    if how == "median":
        return statistics.median(values)
    return statistics.mean(values)


class InvalidSweepError(ValueError):
    """El espacio de búsqueda es inválido (RF-2)."""


# --------------------------------------------------------------------------- modelos


class SweepAxis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    param: str
    type: Literal["int", "float", "categorical"]
    low: float | None = None
    high: float | None = None
    step: float | None = None
    log: bool = False
    choices: list[float | int | str] | None = None

    @model_validator(mode="after")
    def _check(self) -> SweepAxis:
        if self.type == "categorical":
            if not self.choices:
                msg = f"eje {self.param!r}: 'categorical' requiere 'choices' no vacío"
                raise ValueError(msg)
        else:
            if self.low is None or self.high is None:
                msg = f"eje {self.param!r}: requiere 'low' y 'high'"
                raise ValueError(msg)
            if self.low >= self.high:
                msg = f"eje {self.param!r}: 'low' ({self.low}) debe ser < 'high' ({self.high})"
                raise ValueError(msg)
            if self.step is not None and self.step <= 0:
                msg = f"eje {self.param!r}: 'step' debe ser > 0"
                raise ValueError(msg)
        return self


class SweepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axes: list[SweepAxis]

    @model_validator(mode="after")
    def _check(self) -> SweepSpec:
        if not self.axes:
            msg = "sweep: 'axes' no puede estar vacío"
            raise ValueError(msg)
        names = [a.param for a in self.axes]
        if len(names) != len(set(names)):
            msg = "sweep: hay 'param' duplicados"
            raise ValueError(msg)
        return self


class FoldScore(BaseModel):
    fold: int
    in_sample: float | None
    oos: float | None


class OptimizeResult(BaseModel):
    objective: str
    direction: Direction
    best_params: dict[str, Any]
    best_score: float
    folds: list[FoldScore]
    n_trials: int
    n_pruned: int
    seed: int


# --------------------------------------------------------------------------- sweep / folds


def validate_sweep(spec: SweepSpec, kind: str, settings: FinancesSettings) -> None:
    """Comprueba que cada eje existe en el esquema ``Params`` de la estrategia (RF-2).

    Raises:
        InvalidSweepError: SI ``kind`` no tiene esquema o algún eje no es un parámetro.
    """
    infos = {info.kind: info for info in strategy_source.kinds(settings)}
    info = infos.get(kind)
    if info is None or info.params_schema is None:
        msg = f"kind {kind!r} sin esquema de parámetros"
        raise InvalidSweepError(msg)
    props = set(info.params_schema.get("properties", {}))
    unknown = sorted(a.param for a in spec.axes if a.param not in props)
    if unknown:
        msg = f"ejes inexistentes en {kind!r}: {unknown}; válidos: {sorted(props)}"
        raise InvalidSweepError(msg)


def anchored_folds(n_bars: int, n_folds: int, in_sample_ratio: float) -> list[tuple[int, int]]:
    """Folds anclados; devuelve ``(oos_start, oos_end)`` por fold (in-sample = ``[0, oos_start)``).

    In-sample crece desde el inicio; los OOS son contiguos del mismo tamaño al final (RF-4).

    Raises:
        ValueError: SI los parámetros o las barras son insuficientes.
    """
    if n_folds < 1:
        msg = "folds debe ser >= 1"
        raise ValueError(msg)
    if not 0.0 < in_sample_ratio < 1.0:
        msg = "in_sample_ratio debe estar en (0, 1)"
        raise ValueError(msg)
    oos_size = int(n_bars * (1.0 - in_sample_ratio)) // n_folds
    if oos_size < 1 or n_bars - n_folds * oos_size < 1:
        msg = f"insuficientes barras ({n_bars}) para {n_folds} folds con ratio {in_sample_ratio}"
        raise ValueError(msg)
    return [
        (n_bars - (n_folds - i) * oos_size, n_bars - (n_folds - i - 1) * oos_size)
        for i in range(n_folds)
    ]


def _window_value(  # noqa: PLR0913 - ventana + modelo de ejecución, kw-only
    output: EngineOutput,
    lo: int,
    hi: int,
    *,
    objective: str,
    full_initial_cash: float,
    ppy: float,
) -> float:
    """Valor del objetivo sobre la ventana ``[lo, hi)`` (drawdown relativo a la ventana)."""
    equity = output.equity
    window = equity[lo:hi]
    eqc = window.get_column("equity")
    start_cash = full_initial_cash if lo == 0 else float(equity.get_column("equity")[lo - 1])
    seeded = pl.concat([pl.Series("equity", [start_cash], dtype=pl.Float32), eqc])
    peak = seeded.cum_max()[1:]
    window = window.with_columns((eqc / peak - 1.0).cast(pl.Float32).alias("drawdown"))
    t0 = int(equity.get_column("open_time_ms")[lo])
    t1 = int(equity.get_column("open_time_ms")[hi - 1])
    win_trades = output.trades.filter(pl.col("entry_time_ms").is_between(t0, t1))
    metrics = compute_trading_metrics(
        win_trades, window, initial_cash=start_cash, periods_per_year=ppy
    )
    return objective_value(objective, metrics)


# --------------------------------------------------------------------------- estudio


def _suggest(trial: optuna.Trial, axis: SweepAxis) -> Any:  # noqa: ANN401 - escalar del sweep
    if axis.type == "categorical":
        assert axis.choices is not None  # noqa: S101 - garantizado por el validador
        return trial.suggest_categorical(axis.param, axis.choices)
    assert axis.low is not None and axis.high is not None  # noqa: S101
    if axis.type == "int":
        return trial.suggest_int(
            axis.param, int(axis.low), int(axis.high), step=int(axis.step or 1), log=axis.log
        )
    return trial.suggest_float(axis.param, axis.low, axis.high, step=axis.step, log=axis.log)


def optimize(  # noqa: PLR0913 - entradas del modelo de ejecución + búsqueda, kw-only
    *,
    dataset: BacktestDataset,
    kind: str,
    sweep: SweepSpec,
    objective: str = "calmar",
    n_trials: int = 50,
    folds: int = 4,
    in_sample_ratio: float = 0.75,
    fold_agg: FoldAgg = "mean",
    min_trades: int = 10,
    seed: int = 42,
    costs: CostModel,
    size_fraction: float = 1.0,
    initial_cash: float = 10_000.0,
    start_ms: int | None = None,
    end_ms: int | None = None,
    settings: FinancesSettings | None = None,
) -> OptimizeResult:
    """Optimiza los parámetros de ``kind`` sobre ``dataset`` (RF-1…RF-8).

    Raises:
        InvalidSweepError: SI el sweep es inválido (RF-2).
        KeyError: SI ``objective`` no está en el catálogo (RF-5).
        ValueError: SI las barras no alcanzan para el walk-forward (RF-4).
        SourceNotFoundError / EmptyRangeError: datos (RF-4).
    """
    cfg = settings if settings is not None else FinancesSettings()
    if objective not in OBJECTIVES:
        msg = f"objetivo desconocido {objective!r}; válidos: {sorted(OBJECTIVES)}"
        raise KeyError(msg)
    validate_sweep(sweep, kind, cfg)

    candles = read_candles(cfg, dataset, start_ms, end_ms)
    frame = candles.frame
    windows = anchored_folds(candles.n_bars, folds, in_sample_ratio)
    ppy = periods_per_year(dataset.timeframe)
    engine = VectorbtEngine()
    direction = OBJECTIVES[objective].direction
    degenerate = float("-inf") if direction is Direction.MAX else float("inf")
    pruned = 0

    def trial_fn(trial: optuna.Trial) -> float:
        nonlocal pruned
        params: dict[str, Any] = {a.param: _suggest(trial, a) for a in sweep.axes}
        try:
            output = evaluate_params(
                kind=kind,
                params=params,
                candles=frame,
                engine=engine,
                costs=costs,
                size_fraction=size_fraction,
                initial_cash=initial_cash,
                settings=cfg,
            )
        except Exception:  # noqa: BLE001 - un trial inválido se degrada, no aborta (RF-6)
            pruned += 1
            return degenerate
        if output.trades.height < min_trades:
            pruned += 1
            return degenerate
        oos_values = [
            _window_value(
                output, lo, hi, objective=objective, full_initial_cash=initial_cash, ppy=ppy
            )
            for lo, hi in windows
        ]
        score = _aggregate(oos_values, fold_agg)
        if not math.isfinite(score):
            pruned += 1
            return degenerate
        return score

    opt_direction = "maximize" if direction is Direction.MAX else "minimize"
    study = optuna.create_study(
        direction=opt_direction, sampler=optuna.samplers.TPESampler(seed=seed)
    )
    study.optimize(trial_fn, n_trials=n_trials)

    best = study.best_params
    fold_scores = _best_fold_scores(
        best, kind, frame, windows, objective, costs, size_fraction, initial_cash, ppy, cfg
    )
    return OptimizeResult(
        objective=objective,
        direction=direction,
        best_params=best,
        best_score=study.best_value,
        folds=fold_scores,
        n_trials=n_trials,
        n_pruned=pruned,
        seed=seed,
    )


def _best_fold_scores(  # noqa: PLR0913, PLR0917 - desglose del mejor set
    best: Mapping[str, Any],
    kind: str,
    frame: pl.DataFrame,
    windows: Sequence[tuple[int, int]],
    objective: str,
    costs: CostModel,
    size_fraction: float,
    initial_cash: float,
    ppy: float,
    settings: FinancesSettings,
) -> list[FoldScore]:
    """Recalcula in-sample y OOS por fold para el mejor set (reporte, RF-4)."""
    try:
        output = evaluate_params(
            kind=kind,
            params=best,
            candles=frame,
            engine=VectorbtEngine(),
            costs=costs,
            size_fraction=size_fraction,
            initial_cash=initial_cash,
            settings=settings,
        )
    except Exception:  # noqa: BLE001 - sin desglose si el mejor set falla
        return []
    scores: list[FoldScore] = []
    for i, (lo, hi) in enumerate(windows):
        in_sample = _window_value(
            output, 0, lo, objective=objective, full_initial_cash=initial_cash, ppy=ppy
        )
        oos = _window_value(
            output, lo, hi, objective=objective, full_initial_cash=initial_cash, ppy=ppy
        )
        scores.append(
            FoldScore(
                fold=i,
                in_sample=in_sample if math.isfinite(in_sample) else None,
                oos=oos if math.isfinite(oos) else None,
            )
        )
    return scores
