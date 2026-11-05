"""Capa de servicio de ``ft_vectorbt`` compartida por CLI y API (Art. 1; RF-4, RF-5, RF-8).

Adaptador delgado (ADR-V01) sobre el pipeline común
:func:`finances_core.backtest.runner.run_backtest` con :class:`VectorbtEngine`:

    run_sync:  run_backtest(request, VectorbtEngine(), settings, on_phase, task_id=...)
    dispatch:  validar (forma + estrategia, sin crear tarea) → tarea PENDING (UUID v7)
    run_task:  semáforo FT_BACKTEST_MAX_CONCURRENT → asyncio.to_thread(run_sync)

Las fases ``LOADING → SIGNALS → SIMULATING → PERSISTING → COMPLETED``/``FAILED``, el
estado terminal y el evento de auditoría ``backtest_run`` los escribe el ``runner`` del
núcleo (B5.1); este módulo **no** escribe fases. Solo añade el contexto de la corrida
(``task_id``, ``dataset``, ``symbol``, ``timeframe``) con ``structlog.contextvars`` para que
llegue a todos sus logs (RF-10).

Cada despacho genera un ``task_id`` nuevo: una tarea terminal nunca se reutiliza, de modo
que sus fases no se reinician (pendiente anotado en B5.1). Fuera de alcance: si el hilo
muere por un ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) el ``runner`` no
marca la tarea y queda en su última fase no terminal.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import structlog
from finances_core.backtest import strategy_source
from finances_core.backtest.runner import PhaseCallback, run_backtest
from finances_core.config import FinancesSettings
from finances_core.marketdata.plans import new_task_id
from finances_core.schemas.backtest import BacktestRequest, BacktestTaskResult
from finances_core.tasks import TERMINAL_STATUSES, SQLTaskManager

from finances_backtester.ft_vectorbt.engine import VectorbtEngine

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DispatchedRun:
    """Petición validada con su tarea ``PENDING`` ya creada en ``tasks.db``."""

    task_id: str
    request: BacktestRequest


def _settings(settings: FinancesSettings | None) -> FinancesSettings:
    return settings if settings is not None else FinancesSettings()


@contextmanager
def _run_context(request: BacktestRequest, task_id: str | None) -> Iterator[None]:
    """Contexto RF-10 de la corrida en ``structlog.contextvars`` (solo mientras dura).

    ``run_id``, ``kind`` y ``strategy_code_hash`` se conocen dentro del ``runner`` y viajan
    en el propio evento ``backtest_run``.
    """
    context: dict[str, str] = {
        "dataset": request.dataset.path_segment(),
        "symbol": request.dataset.symbol,
        "timeframe": request.dataset.timeframe,
    }
    if task_id is not None:
        context["task_id"] = task_id
    with structlog.contextvars.bound_contextvars(**context):
        yield


def run_sync(
    request: BacktestRequest,
    settings: FinancesSettings | None = None,
    on_phase: PhaseCallback | None = None,
    *,
    task_id: str | None = None,
) -> BacktestTaskResult:
    """Ejecuta la corrida en el hilo actual y devuelve su :class:`BacktestTaskResult`.

    Con ``task_id`` el ``runner`` persiste fases y estado terminal en ``SQLTaskManager``.

    Raises:
        BacktestError: los errores tipados de ``run_backtest`` (RF-9 / RF-14).
        OSError: error de disco. La tarea, si la hay, queda ``FAILED``.
    """
    cfg = _settings(settings)
    with _run_context(request, task_id):
        return run_backtest(request, VectorbtEngine(), cfg, on_phase, task_id=task_id)


def validate_request(request: BacktestRequest, settings: FinancesSettings | None = None) -> None:
    """Comprobaciones baratas y deterministas previas a crear la tarea (RF-7, RF-8).

    La forma (esquema, costes, rango, dataset sin *path traversal*) ya la valida
    :class:`BacktestRequest`; aquí se resuelve la estrategia (``strategies.db``, ``kind``
    registrado y ``params``). El dataset se lee dentro de la tarea. ``strategy_source.load``
    usa el registro del proceso para ``FinancesSettings()`` (el motor no puede importar
    ``catalog``, RF-15); coincide con el del ``runner`` mientras ``settings`` provenga del
    entorno, como en CLI y API.

    Raises:
        StrategyNotFoundError / StrategyVersionNotFoundError: estrategia inexistente (404).
        UnknownKindError / InvalidParamsError: ``parameters`` inválidos (400).
        StrategyInitError: estrategia local inválida (500).
    """
    cfg = _settings(settings)
    strategy_source.load(
        request.strategy_id, request.strategy_version, db_path=cfg.STRATEGIES_DB_PATH
    )


def dispatch(request: BacktestRequest, settings: FinancesSettings | None = None) -> DispatchedRun:
    """Valida la petición y crea su tarea ``PENDING`` con un ``task_id`` UUID v7 nuevo.

    No calcula nada: la corrida la ejecuta :func:`run_task` (p. ej. en ``BackgroundTasks``).
    Una petición inválida lanza su error **sin** crear tarea.

    Raises:
        Los de :func:`validate_request`.
    """
    cfg = _settings(settings)
    validate_request(request, cfg)
    task_id = new_task_id()
    SQLTaskManager(cfg.TASKS_DB_PATH).create_task(task_id, "PENDING")
    logger.info(
        "backtest_dispatched",
        task_id=task_id,
        dataset=request.dataset.path_segment(),
        strategy_id=request.strategy_id,
        strategy_version=request.strategy_version,
        forced=request.force,
    )
    return DispatchedRun(task_id=task_id, request=request)


# Un semáforo por bucle de eventos y límite: a lo sumo FT_BACKTEST_MAX_CONCURRENT corridas
# simultáneas por proceso (RF-4). Un asyncio.Semaphore queda ligado a su bucle, por eso
# no puede ser un único objeto de módulo (p. ej. un bucle por test o por asyncio.run).
_semaphores_lock = threading.Lock()
_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]]
_semaphores = weakref.WeakKeyDictionary()


def _semaphore(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _semaphores_lock:
        per_loop = _semaphores.setdefault(loop, {})
        semaphore = per_loop.get(limit)
        if semaphore is None:
            semaphore = per_loop[limit] = asyncio.Semaphore(limit)
    return semaphore


def _fail_unfinished(cfg: FinancesSettings, task_id: str, error: str) -> bool:
    """Marca ``FAILED`` una tarea que quedó sin estado terminal; ``True`` si la marcó."""
    tasks = SQLTaskManager(cfg.TASKS_DB_PATH)
    row = tasks.get_task(task_id)
    if row is not None and row["status"] in TERMINAL_STATUSES:
        return False
    tasks.update_task(task_id, status="FAILED", error=error, errors_json=[error])
    return True


async def run_task(
    dispatched: DispatchedRun, settings: FinancesSettings | None = None
) -> BacktestTaskResult | None:
    """Ejecuta una tarea despachada en un hilo, bajo el semáforo del proceso (Art. 3).

    Mientras espera turno la tarea sigue ``PENDING``. El ``runner`` registra las fases y
    el estado terminal; ante un fallo la tarea queda ``FAILED`` (con su error) y se
    devuelve ``None`` en lugar de propagar la excepción, que ya quedó en el log y en la
    auditoría (una tarea en segundo plano no tiene a quién devolverla). Un fallo antes de
    que el ``runner`` registre la tarea (p. ej. al construir el motor) y una cancelación
    mientras espera turno también la dejan ``FAILED``: nunca queda ``PENDING`` para siempre.
    Una cancelación con la simulación ya en curso no la interrumpe: el hilo termina y el
    ``runner`` registra su estado.
    """
    cfg = _settings(settings)
    started = False
    try:
        async with _semaphore(cfg.BACKTEST_MAX_CONCURRENT):
            started = True
            return await asyncio.to_thread(
                run_sync, dispatched.request, cfg, task_id=dispatched.task_id
            )
    except asyncio.CancelledError:
        if not started:
            _fail_unfinished(cfg, dispatched.task_id, "cancelled: cancelada antes de ejecutarse")
        raise
    except Exception as exc:  # noqa: BLE001 - una tarea en segundo plano no propaga
        error = f"{type(exc).__name__}: {exc}"
        if _fail_unfinished(cfg, dispatched.task_id, error):
            # Fallo fuera del runner: ni log ni FAILED registrados todavía.
            logger.exception("backtest_task_failed", task_id=dispatched.task_id, error=error)
        return None
