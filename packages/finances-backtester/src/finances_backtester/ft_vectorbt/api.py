"""Router FastAPI ``/backtest/vectorbt`` de ``ft_vectorbt`` (RF-7, RF-8, RF-9; plan §2.2).

Lo monta ``finances-api`` (``ft_serve``). Las rutas llevan nombres únicos con el prefijo
``vectorbt_*`` para que ``url_for`` resuelva las URLs de *polling* hacia ``/backtest/vectorbt``
sin colisiones en la app compuesta (plan §2.2).

- ``POST /run`` valida la petición de forma síncrona y barata (esquema por ``BacktestRequest``;
  estrategia, ``kind`` y ``params`` por :func:`service.dispatch`), crea la tarea ``PENDING`` y
  despacha la corrida en ``BackgroundTasks`` respondiendo ``202`` sin calcular (Art. 18). La
  lectura de datos y el cálculo de ``run_id`` ocurren dentro de la tarea.
- ``GET /status/{task_id}`` devuelve estado y fase; ``GET /result/{task_id}`` el
  :class:`BacktestTaskResult` (``409`` si la tarea no terminó, ``404`` si no existe, ``500`` si
  ``FAILED``). Las lecturas de SQLite se delegan a ``run_in_threadpool`` (Art. 3).
- Los endpoints de solo lectura de RF-7 (V4.2): ``GET /kinds`` (``KindInfo``; sin ningún
  endpoint que reciba código, rutas ni módulos, RF-18), ``GET /runs`` (filtros y cursor),
  ``GET /runs/{run_id}`` (manifiesto + resumen, ``404``) y ``GET /runs/{run_id}/trades`` y
  ``/equity`` (paginados: ``limit`` 1..10000 por ``Query``, ``after_ms``, ``next_after_ms``).
  Todas las lecturas de SQLite/Parquet se delegan a ``run_in_threadpool`` para no bloquear el
  *event loop* en lecturas grandes (Art. 3).
- Los errores de dominio de RF-9 se traducen con :func:`_http_error`; los errores de esquema de
  la petición responden ``422`` por el comportamiento por defecto de FastAPI; un ``cursor`` mal
  formado (``ValueError`` de ``service.list_runs``) responde ``422``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from fastapi import Path as PathParam
from finances_core.backtest.errors import (
    InvalidParamsError,
    StrategyInitError,
    StrategyNotFoundError,
    UnknownKindError,
)
from finances_core.config import FinancesSettings
from finances_core.marketdata import tasks_lifecycle
from finances_core.marketdata.tasks_lifecycle import TaskNotFinishedError
from finances_core.schemas.backtest import (
    BacktestDispatchResponse,
    BacktestRequest,
    BacktestTaskResult,
    EquityPage,
    KindInfo,
    RunManifest,
    RunsPage,
    RunSummary,
    TradesPage,
)
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import service

#: ``task_id`` UUID v7 en la ruta (mismo patrón que ``ft_ta_lib``; 36 chars hex y guiones).
TASK_ID_PATTERN = r"^[0-9a-f-]{36}$"

#: Nombres de ruta únicos en la app compuesta (``url_for`` resuelve por nombre, plan §2.2).
RUN_ROUTE = "vectorbt_run"
STATUS_ROUTE = "vectorbt_status"
RESULT_ROUTE = "vectorbt_result"
KINDS_ROUTE = "vectorbt_kinds"
RUNS_ROUTE = "vectorbt_runs"
RUN_DETAIL_ROUTE = "vectorbt_run_detail"
TRADES_ROUTE = "vectorbt_trades"
EQUITY_ROUTE = "vectorbt_equity"

router = APIRouter(prefix="/backtest/vectorbt", tags=["ft_vectorbt"])


class BacktestTaskStatus(BaseModel):
    """Estado determinista de una tarea despachada (``GET /status/{task_id}``, RF-8).

    Vista estable de la fila de ``tasks.db``: ``status`` y ``phase`` de las fases
    ``PENDING → LOADING → SIGNALS → SIMULATING → PERSISTING → COMPLETED``/``FAILED``, el
    ``error`` (si lo hay) y el ``summary`` (``run_id``, ``cached``, ``n_bars``, ``n_trades``).
    """

    task_id: str
    status: str
    phase: str | None = None
    error: str | None = None
    summary: dict[str, Any] = {}


class RunDetail(BaseModel):
    """Manifiesto + resumen de una corrida almacenada (``GET /runs/{run_id}``, RF-7).

    Misma forma que produce el comando CLI ``show`` (``{manifest, summary}``); no existe un
    esquema combinado en el núcleo, así que se define aquí (Art. 1, dualidad CLI/API).
    """

    manifest: RunManifest
    summary: RunSummary


def _http_error(exc: Exception) -> HTTPException:
    """Errores de dominio → códigos HTTP de RF-9 (tabla de ``finances_backtester`` RF-14)."""
    if isinstance(exc, StrategyNotFoundError):  # incluye StrategyVersionNotFoundError
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, UnknownKindError | InvalidParamsError):
        code = status.HTTP_400_BAD_REQUEST
    elif isinstance(exc, TaskNotFinishedError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, StrategyInitError | service.TaskFailedError):
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:  # excepción inesperada del motor u otra
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    return HTTPException(status_code=code, detail=str(exc))


@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BacktestDispatchResponse,
    name=RUN_ROUTE,
)
async def run_endpoint(
    body: BacktestRequest, request: Request, background_tasks: BackgroundTasks
) -> BacktestDispatchResponse:
    """Despacha la corrida y responde ``202`` con ``task_id`` y URLs de *polling* (Art. 18).

    :func:`service.dispatch` valida (forma, estrategia, ``kind``, ``params``) y crea la tarea
    ``PENDING`` sin calcular; una petición inválida se traduce con :func:`_http_error`
    (``404``/``400``/``500``) **sin** crear la tarea. El cálculo corre en ``BackgroundTasks``.
    """
    settings = FinancesSettings()
    try:
        dispatched = service.dispatch(body, settings)
    except (
        StrategyNotFoundError,
        UnknownKindError,
        InvalidParamsError,
        StrategyInitError,
    ) as exc:
        raise _http_error(exc) from exc
    background_tasks.add_task(service.run_task, dispatched, settings)
    task_id = dispatched.task_id
    return BacktestDispatchResponse(
        task_id=task_id,
        poll_url=str(request.url_for(STATUS_ROUTE, task_id=task_id)),
        result_url=str(request.url_for(RESULT_ROUTE, task_id=task_id)),
    )


@router.get("/status/{task_id}", response_model=BacktestTaskStatus, name=STATUS_ROUTE)
async def status_endpoint(
    task_id: str = PathParam(..., pattern=TASK_ID_PATTERN),
) -> BacktestTaskStatus:
    """Estado y fase de una tarea despachada (``404`` si no existe, RF-8)."""
    settings = FinancesSettings()
    row = await run_in_threadpool(service.task_status, task_id, settings)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return BacktestTaskStatus(
        task_id=task_id,
        status=str(row["status"]),
        phase=row.get("phase"),
        error=row.get("error"),
        summary=tasks_lifecycle.json_field(row, "result_summary_json", {}),
    )


@router.get("/result/{task_id}", response_model=BacktestTaskResult, name=RESULT_ROUTE)
async def result_endpoint(
    task_id: str = PathParam(..., pattern=TASK_ID_PATTERN),
) -> BacktestTaskResult:
    """Resultado de una tarea terminada (``409`` sin terminar, ``404`` inexistente, ``500``
    ``FAILED``; RF-9)."""
    settings = FinancesSettings()
    try:
        result = await run_in_threadpool(service.task_result, task_id, settings)
    except (TaskNotFinishedError, service.TaskFailedError) as exc:
        raise _http_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return result


# --------------------------------------------- lecturas de solo lectura (RF-7, RF-9; V4.2)


@router.get("/kinds", response_model=list[KindInfo], name=KINDS_ROUTE)
async def kinds_endpoint(
    kind: str | None = Query(None, description="Filtrar por un kind (espejo de la CLI)"),
) -> list[KindInfo]:
    """Estrategias registradas (``KindInfo``, RF-1); solo lectura, sin cargar código (RF-18).

    No existe ningún endpoint que reciba código, rutas ni módulos. El filtro opcional ``kind``
    reproduce la CLI. La lectura del registro corre en el *threadpool* (Art. 3).
    """
    settings = FinancesSettings()
    infos = await run_in_threadpool(service.kinds, settings)
    if kind is not None:
        infos = [info for info in infos if info.kind == kind]
    return infos


@router.get("/runs", response_model=RunsPage, name=RUNS_ROUTE)
async def runs_endpoint(  # noqa: PLR0913 - filtros de RF-6/RF-9 con nombre
    exchange: str | None = Query(None),
    market_type: str | None = Query(None),
    symbol: str | None = Query(None),
    timeframe: str | None = Query(None),
    strategy_id: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(100, ge=1, le=10000),
) -> RunsPage:
    """Corridas almacenadas con filtros y paginación por cursor (RF-7, RF-9).

    ``limit`` se acota en 1..10000 por ``Query`` (``422`` fuera de rango); un ``cursor`` mal
    formado hace que ``service.list_runs`` lance ``ValueError``, traducido a ``422``. La
    lectura de disco corre en el *threadpool* (Art. 3).
    """
    settings = FinancesSettings()
    try:
        return await run_in_threadpool(
            lambda: service.list_runs(
                exchange=exchange,
                market_type=market_type,
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                cursor=cursor,
                limit=limit,
                settings=settings,
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.get("/runs/{run_id}", response_model=RunDetail, name=RUN_DETAIL_ROUTE)
async def run_detail_endpoint(run_id: str) -> RunDetail:
    """Manifiesto + resumen de una corrida (``404`` si no existe, RF-7).

    Misma forma que el comando CLI ``show``; la lectura de disco corre en el *threadpool*.
    """
    settings = FinancesSettings()
    stored = await run_in_threadpool(service.find_run, run_id, settings)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return RunDetail(manifest=stored.manifest, summary=stored.summary)


@router.get("/runs/{run_id}/trades", response_model=TradesPage, name=TRADES_ROUTE)
async def trades_endpoint(
    run_id: str,
    after_ms: int | None = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
) -> TradesPage:
    """Página de ``trades`` de ``run_id`` (``404`` si no existe, RF-9).

    ``limit`` acotado 1..10000 por ``Query``; la lectura de Parquet corre en el *threadpool*.
    """
    settings = FinancesSettings()
    page = await run_in_threadpool(
        lambda: service.read_trades(run_id, after_ms=after_ms, limit=limit, settings=settings)
    )
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return page


@router.get("/runs/{run_id}/equity", response_model=EquityPage, name=EQUITY_ROUTE)
async def equity_endpoint(
    run_id: str,
    after_ms: int | None = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
) -> EquityPage:
    """Página de ``equity`` de ``run_id`` (``404`` si no existe, RF-9).

    ``limit`` acotado 1..10000 por ``Query``; la lectura de Parquet corre en el *threadpool*.
    """
    settings = FinancesSettings()
    page = await run_in_threadpool(
        lambda: service.read_equity(run_id, after_ms=after_ms, limit=limit, settings=settings)
    )
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return page


__all__ = ["TASK_ID_PATTERN", "BacktestTaskStatus", "RunDetail", "router"]
