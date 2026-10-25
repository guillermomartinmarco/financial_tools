"""Router FastAPI ``/market-data/yfinance`` (lo monta ``finances-api``; aquí no hay app).

- ``POST /download`` valida (sin red), registra la tarea ``PENDING`` y responde
  ``202`` de inmediato; planificación y descarga corren en ``BackgroundTasks``
  dentro del *threadpool* con su propio event loop (Art. 3, Art. 18).
- Las lecturas SQLite / disco de los ``GET`` se delegan a ``run_in_threadpool``.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Query, Request, status
from finances_core.schemas.dispatch import DispatchResponse
from finances_core.schemas.market_data_yfinance import (
    YFDownloadPlan,
    YFDownloadProgress,
    YFDownloadRequest,
    YFDownloadResult,
)
from finances_core.timeutils import parse_time_input
from starlette.concurrency import run_in_threadpool

from finances_downloader.ft_market_data_yfinance.planner import load_plan, validate_request
from finances_downloader.ft_market_data_yfinance.service import (
    TaskNotFinishedError,
    create_download_task,
    get_progress,
    get_result,
    new_task_id,
    run_download_sync,
)

router = APIRouter(prefix="/market-data/yfinance", tags=["market-data-yfinance"])

TASK_ID_PATTERN = r"^[0-9a-f-]{36}$"


@router.post("/download", status_code=status.HTTP_202_ACCEPTED, response_model=DispatchResponse)
async def download_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    symbols: list[str] = Query(..., min_length=1, max_length=100),
    timeframes: list[str] = Query(..., min_length=1),
    start: str = Query(..., description="Inicio: ISO 8601, ms epoch o 'now'"),
    end: str | None = Query(None, description="Fin: ISO 8601, ms epoch o 'now' (default ahora)"),
    max_concurrent: int | None = Query(None, ge=1, le=50),
) -> DispatchResponse:
    """Despacha la descarga y retorna ``task_id`` + URLs de polling sin esperar."""
    try:
        download_request = YFDownloadRequest.model_validate(
            {
                "symbols": symbols,
                "timeframes": timeframes,
                "start_ms": parse_time_input(start),
                "end_ms": parse_time_input(end) if end else None,
                "max_concurrent": max_concurrent,
            }
        )
        validate_request(download_request)
    except ValueError as exc:  # incluye pydantic.ValidationError (RF-1 paso 1 -> 400)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    task_id = new_task_id()
    await run_in_threadpool(create_download_task, task_id, download_request)
    background_tasks.add_task(run_download_sync, download_request, task_id)
    return DispatchResponse(
        task_id=task_id,
        poll_url=str(request.url_for("status_endpoint", task_id=task_id)),
        plan_url=str(request.url_for("plan_endpoint", task_id=task_id)),
        result_url=str(request.url_for("result_endpoint", task_id=task_id)),
    )


@router.get("/status/{task_id}", response_model=YFDownloadProgress)
async def status_endpoint(
    task_id: str = Path(..., pattern=TASK_ID_PATTERN),
) -> YFDownloadProgress:
    """Progreso por fase (RF-8)."""
    progress = await run_in_threadpool(get_progress, task_id)
    if progress is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return progress


@router.get("/plan/{task_id}", response_model=YFDownloadPlan)
async def plan_endpoint(task_id: str = Path(..., pattern=TASK_ID_PATTERN)) -> YFDownloadPlan:
    """Plan de descarga persistido (ventanas y ``gaps_unfillable``)."""
    plan = await run_in_threadpool(load_plan, task_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return plan


@router.get("/result/{task_id}", response_model=YFDownloadResult)
async def result_endpoint(task_id: str = Path(..., pattern=TASK_ID_PATTERN)) -> YFDownloadResult:
    """Resultado final: rutas Parquet y de eventos, stats, errores y ``gaps_unfillable``."""
    try:
        result = await run_in_threadpool(get_result, task_id)
    except TaskNotFinishedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Task not finished: {exc.status}"
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return result
