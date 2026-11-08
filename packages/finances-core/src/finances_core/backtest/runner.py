"""Orquestación común de una corrida de backtest (RF-3..RF-10, RF-13; plan §3.1, ADR-B01).

:func:`run_backtest` es el único pipeline de todos los motores; el ``service.py`` de cada
``ft_<motor>`` solo aporta su :class:`~finances_core.backtest.engine_api.BacktestEngine`::

    estrategia (strategy_source) → velas (read_candles) → identidad (data_hash, run_id)
      → ¿existe? (find_run; sin calcular señales) → señales (Strategy.signals + finalize)
      → engine.run → validate_engine_output → metrics.compute → write_run (atómico)

Fases reportables ``PENDING → LOADING → SIGNALS → SIMULATING → PERSISTING → COMPLETED``
(o ``FAILED``), siempre hacia delante; con ``task_id`` se persisten en ``SQLTaskManager``
(Art. 2) y ``on_phase`` recibe cada una. Un resultado en caché pasa de ``LOADING`` a
``COMPLETED``.

Es síncrona: quien despacha la ejecuta en un hilo (``asyncio.to_thread``, Art. 3). Ante
cualquier fallo la tarea queda ``FAILED``, se emite la auditoría con ``result = "failed"``
y la excepción tipada se propaga **sin escribir nada** en ``FT_RESULTS_DIR``: la salida
del motor se valida antes de escribir y ``write_run`` es atómico.
"""

from __future__ import annotations

import platform
import time
from collections.abc import Callable
from typing import Any

import polars as pl
import pydantic
import structlog

from finances_core.backtest import identity, metrics, results_store, strategy_source
from finances_core.backtest.candles import Candles, read_candles
from finances_core.backtest.catalog import (
    CATALOG_VERSION,
    RESULT_SCHEMA_VERSION,
    StrategyRegistry,
    get_registry,
)
from finances_core.backtest.engine_api import BacktestEngine, EngineOutput
from finances_core.backtest.errors import BacktestError, StrategyRuntimeError
from finances_core.backtest.frames import validate_engine_output
from finances_core.backtest.strategies.base import Signals
from finances_core.backtest.strategies.signals import finalize
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import (
    BacktestRequest,
    BacktestTaskResult,
    CostModel,
    ResolvedStrategy,
    RunManifest,
    RunSummary,
)
from finances_core.tasks import SQLTaskManager, now_ms

logger = structlog.get_logger(__name__)

PhaseCallback = Callable[[str], None]

#: Fases no terminales en orden (plan §3.1); ``COMPLETED``/``FAILED`` cierran la tarea.
PHASE_ORDER = ("PENDING", "LOADING", "SIGNALS", "SIMULATING", "PERSISTING")
COMPLETED = "COMPLETED"
FAILED = "FAILED"

#: Advertencia de RF-4: menos barras que ``warmup + 2``.
INSUFFICIENT_HISTORY = "insufficient_history"
#: Barras mínimas tras el calentamiento para poder abrir y cerrar una operación (RF-4).
MIN_BARS_AFTER_WARMUP = 2


class _Phases:
    """Avance monótono de fases, persistido en ``SQLTaskManager`` si hay ``task_id``."""

    def __init__(
        self, task_id: str | None, settings: FinancesSettings, on_phase: PhaseCallback | None
    ) -> None:
        self.task_id = task_id
        self.on_phase = on_phase
        self.manager: SQLTaskManager | None = None
        self._index = 0
        if task_id is not None:
            self.manager = SQLTaskManager(settings.TASKS_DB_PATH)
            self.manager.create_task(task_id, "PENDING")  # idempotente

    def _notify(self, phase: str, **fields: Any) -> None:
        if self.manager is not None and self.task_id is not None:
            self.manager.update_task(self.task_id, status=phase, phase=phase, **fields)
            logger.info("task_phase", task_id=self.task_id, phase=phase)
        if self.on_phase is not None:
            self.on_phase(phase)

    def advance(self, phase: str) -> None:
        index = PHASE_ORDER.index(phase)
        if index > self._index:
            self._index = index
            self._notify(phase)

    def complete(self, summary: dict[str, Any]) -> None:
        self._index = len(PHASE_ORDER)
        self._notify(COMPLETED, result_summary_json=summary)

    def fail(self, message: str) -> None:
        self._index = len(PHASE_ORDER)
        self._notify(FAILED, error=message, errors_json=[message])


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


class _Audit:
    """Campos del evento de auditoría ``backtest_run`` (RF-13), rellenados por etapa."""

    def __init__(
        self, task_id: str | None, request: BacktestRequest, engine: BacktestEngine
    ) -> None:
        self.fields: dict[str, Any] = {
            "task_id": task_id,
            "run_id": None,
            "engine": engine.name,
            "engine_version": None,
            "strategy_id": request.strategy_id,
            "strategy_version": request.strategy_version,
            "strategy_hash": None,
            "kind": None,
            "kind_version": None,
            "strategy_code_hash": None,
            "strategy_origin": None,
            "dataset": request.dataset.path_segment(),
            "symbol": request.dataset.symbol,
            "timeframe": request.dataset.timeframe,
            "start_ms": request.start_ms,
            "end_ms": request.end_ms,
            "n_bars": None,
            "n_trades": None,
            "cached": False,
            "forced": request.force,
            "load_ms": None,
            "signals_ms": None,
            "engine_ms": None,
            "persist_ms": None,
        }

    def strategy(self, strategy: ResolvedStrategy) -> None:
        self.fields.update(
            strategy_version=strategy.version,
            strategy_hash=strategy.hash,
            kind=strategy.kind,
            kind_version=strategy.kind_version,
            strategy_code_hash=strategy.code_hash,
            strategy_origin=strategy.origin,
        )

    def emit(self, result: str, *, started: float, **extra: Any) -> None:
        logger.info(
            "backtest_run",
            **self.fields,
            duration_ms=_ms(time.perf_counter() - started),
            result=result,
            **extra,
        )


def _error_message(exc: BaseException) -> str:
    code = str(getattr(exc, "code", type(exc).__name__))
    text = str(exc)
    return text if text.startswith(f"{code}:") else f"{code}: {text}"


def _compute_signals(
    registry: StrategyRegistry, strategy: ResolvedStrategy, frame: pl.DataFrame
) -> tuple[pl.Series, int]:
    """Señales normalizadas (RF-16) y ``warmup`` de la estrategia resuelta.

    Raises:
        StrategyRuntimeError: SI ``warmup``/``signals`` lanzan una excepción.
        InvalidSignalsError: SI la salida no es casteable a Int8, de longitud ``n``
            y sin nulos en ``t >= warmup``.
    """
    strategy_cls = registry.get(strategy.kind)
    params = strategy_cls.Params.model_validate(strategy.params)
    try:
        warmup = int(strategy_cls.warmup(params))
        raw = strategy_cls.signals(frame, params)
    except Exception as exc:
        logger.exception(
            "strategy_runtime_error",
            kind=strategy.kind,
            strategy_origin=strategy.origin,
            error_type=type(exc).__name__,
        )
        raise StrategyRuntimeError(strategy.kind, exc) from exc
    return finalize(raw, n=frame.height, warmup=warmup), warmup


def _warnings(candles: Candles, warmup: int, output: EngineOutput) -> list[str]:
    found: list[str] = []
    if candles.n_bars < warmup + MIN_BARS_AFTER_WARMUP:
        found.append(INSUFFICIENT_HISTORY)
    if candles.data_gaps:
        found.append(f"data_gaps={candles.data_gaps}")
    found.extend(output.warnings)
    return list(dict.fromkeys(found))  # sin duplicados, en orden de aparición


def _task_summary(run_id: str, summary: RunSummary, *, cached: bool) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "cached": cached,
        "n_bars": summary.n_bars,
        "n_trades": summary.n_trades,
    }


def run_backtest(  # noqa: PLR0913 - entradas de RF-10 más task_id y registro
    request: BacktestRequest,
    engine: BacktestEngine,
    settings: FinancesSettings,
    on_phase: PhaseCallback | None = None,
    *,
    task_id: str | None = None,
    registry: StrategyRegistry | None = None,
) -> BacktestTaskResult:
    """Ejecuta una corrida completa y devuelve su :class:`BacktestTaskResult`.

    Args:
        request: petición canónica (RF-3).
        engine: motor que solo simula (RF-10, RF-21).
        settings: ``FT_PARQUET_DIR``, ``FT_RESULTS_DIR``, ``FT_TASKS_DB_PATH``,
            ``FT_STRATEGIES_DB_PATH`` y ``FT_STRATEGIES_DIR`` (vía el registro).
        on_phase: recibe cada fase alcanzada (incluidas ``COMPLETED``/``FAILED``).
        task_id: si se da, la tarea se crea (idempotente) y sus fases y resultado se
            persisten en ``SQLTaskManager``.
        registry: registro de estrategias; por defecto el del proceso para ``settings``.

    Raises:
        StrategyNotFoundError / StrategyVersionNotFoundError: estrategia inexistente (404).
        UnknownKindError / InvalidParamsError: ``parameters`` inválidos (400).
        StrategyInitError: estrategia local inválida (500).
        SourceNotFoundError / InvalidSourceError / EmptyRangeError: datos (RF-4).
        StrategyRuntimeError / InvalidSignalsError: fallo de las señales (RF-16).
        OutputOutOfRangeError: salida del motor no canónica (RF-6).
        OSError: error de disco. En todos los casos la tarea queda ``FAILED`` y no se
            escribe ningún resultado.
    """
    started = time.perf_counter()
    phases = _Phases(task_id, settings, on_phase)
    audit = _Audit(task_id, request, engine)
    try:
        result = _run(request, engine, settings, phases, audit, registry, started)
    except Exception as exc:
        message = _error_message(exc)
        phases.fail(message)
        if isinstance(exc, BacktestError):
            logger.warning("backtest_run_failed", task_id=task_id, error=message)
        else:  # excepción inesperada del motor, IO…: traza solo en el log
            logger.exception("backtest_run_failed", task_id=task_id, error=message)
        audit.emit("failed", started=started, error=message)
        raise
    phases.complete(_task_summary(result.run_id, result.summary, cached=result.cached))
    audit.emit("cached" if result.cached else "completed", started=started)
    return result


def _run(  # noqa: PLR0913, PLR0917 - estado interno de run_backtest
    request: BacktestRequest,
    engine: BacktestEngine,
    settings: FinancesSettings,
    phases: _Phases,
    audit: _Audit,
    registry: StrategyRegistry | None,
    started: float,
) -> BacktestTaskResult:
    fields = audit.fields
    phases.advance("LOADING")
    active = registry if registry is not None else get_registry(settings)
    strategy = strategy_source.load(
        request.strategy_id,
        request.strategy_version,
        registry=active,
        db_path=settings.STRATEGIES_DB_PATH,
    )
    audit.strategy(strategy)
    candles = read_candles(settings, request.dataset, request.start_ms, request.end_ms)
    data_hash = identity.data_hash(candles.frame)
    engine_version = engine.version()
    run_id = identity.run_id(
        request,
        strategy,
        start_ms=candles.start_ms,
        end_ms=candles.end_ms,
        data_hash=data_hash,
        engine=engine.name,
        engine_version=engine_version,
    )
    fields.update(
        run_id=run_id,
        engine_version=engine_version,
        start_ms=candles.start_ms,
        end_ms=candles.end_ms,
        n_bars=candles.n_bars,
        load_ms=_ms(time.perf_counter() - started),
    )

    # RF-8: un resultado completo se devuelve sin calcular señales ni invocar al motor.
    if not request.force:
        stored = results_store.find_run(settings.RESULTS_DIR, run_id)
        if stored is not None:
            fields.update(cached=True, n_trades=stored.summary.n_trades)
            return BacktestTaskResult(
                run_id=run_id, cached=True, summary=stored.summary, manifest=stored.manifest
            )

    phases.advance("SIGNALS")
    mark = time.perf_counter()
    pos, warmup = _compute_signals(active, strategy, candles.frame)
    # Derivar flancos para el motor largo/plano (G2a)
    from finances_core.backtest.strategies.signals import position_to_signals
    signals = position_to_signals(pos)
    fields["signals_ms"] = _ms(time.perf_counter() - mark)

    phases.advance("SIMULATING")
    mark = time.perf_counter()
    costs = CostModel(fees_bps=request.fees_bps, slippage_bps=request.slippage_bps)
    output = engine.run(candles.frame, signals, costs, request.size_fraction, request.initial_cash)
    fields["engine_ms"] = _ms(time.perf_counter() - mark)
    validate_engine_output(output, candles.n_bars)
    summary = metrics.compute(
        output.trades,
        output.equity,
        initial_cash=request.initial_cash,
        timeframe=candles.dataset.timeframe,
    )
    fields["n_trades"] = summary.n_trades

    phases.advance("PERSISTING")
    manifest = RunManifest(
        run_id=run_id,
        engine=engine.name,
        engine_version=engine_version,
        catalog_version=CATALOG_VERSION,
        result_schema_version=RESULT_SCHEMA_VERSION,
        request=request,
        strategy=strategy,
        start_ms=candles.start_ms,
        end_ms=candles.end_ms,
        n_bars=candles.n_bars,
        data_hash=data_hash,
        data_gaps=candles.data_gaps,
        created_at_ms=now_ms(),
        duration_ms=int((time.perf_counter() - started) * 1000),
        warnings=_warnings(candles, warmup, output),
        python_version=platform.python_version(),
        libraries={"polars": pl.__version__, "pydantic": pydantic.VERSION},
    )
    mark = time.perf_counter()
    stored = results_store.write_run(
        settings.RESULTS_DIR,
        manifest=manifest,
        summary=summary,
        trades=output.trades,
        equity=output.equity,
        force=request.force,
    )
    fields["persist_ms"] = _ms(time.perf_counter() - mark)
    # Otra tarea con el mismo run_id pudo instalar antes un resultado idéntico (RF-8).
    return BacktestTaskResult(
        run_id=run_id, cached=False, summary=stored.summary, manifest=stored.manifest
    )
