import json

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Query, Request, status
from finances_core.config import settings
from finances_core.schemas.market_data import (
    DownloadPlan,
    DownloadProgress,
    DownloadRequest,
    DownloadResult,
    MarketType,
    Timeframe,
)
from finances_core.tasks import SQLTaskManager

from finances_downloader.ft_market_data_binance.planner import generate_download_plan
from finances_downloader.ft_market_data_binance.service import execute_download_plan

router = APIRouter(prefix="/market-data/binance", tags=["market-data-binance"])
logger = structlog.get_logger(__name__)


@router.post("/download", status_code=status.HTTP_202_ACCEPTED)
async def download_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    symbols: list[str] = Query(..., min_length=1, max_length=100),
    timeframes: list[str] = Query(..., min_length=1),
    start_ms: int = Query(..., gt=0, description="Timestamp inicio en ms UTC"),
    end_ms: int | None = Query(None, gt=0, description="Timestamp fin en ms UTC"),
    market_type: str = Query("spot", pattern="^(spot|futures)$"),
    max_concurrent: int | None = Query(None, ge=1, le=50),
    limit_per_request: int = Query(1000, ge=1, le=1000),
) -> dict[str, str]:
    """
    Inicia descarga en background.
    Retorna: {"task_id": "<uuid-v7>", "status": "ACCEPTED", "poll_url": "/market-data/binance/status/<task_id>"}
    """
    try:
        tf_enums = [Timeframe(tf) for tf in timeframes]
        market_enum = MarketType(market_type)

        concurrent = max_concurrent or settings.DOWNLOAD_MAX_CONCURRENT

        request_obj = DownloadRequest(
            symbols=symbols,
            timeframes=tf_enums,
            start_ms=start_ms,
            end_ms=end_ms,
            market_type=market_enum,
            limit_per_request=limit_per_request,
            max_concurrent=concurrent,
        )

        # Generar plan primero (síncrono, rápido)
        plan = await generate_download_plan(request_obj)

        # Ejecutar en background
        async def run_download() -> None:
            try:
                await execute_download_plan(plan, concurrent)
            except Exception as e:
                logger.error("background_download_failed", task_id=plan.task_id, error=str(e))

        background_tasks.add_task(run_download)

        return {
            "task_id": plan.task_id,
            "status": "ACCEPTED",
            "poll_url": str(request.url_for("status_endpoint", task_id=plan.task_id)),
            "plan_url": str(request.url_for("plan_endpoint", task_id=plan.task_id)),
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("download_endpoint_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/status/{task_id}", response_model=DownloadProgress)
async def status_endpoint(
    task_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
) -> DownloadProgress:
    """Progreso granular de descarga."""
    task_manager = SQLTaskManager(settings.TASKS_DB_PATH)
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return DownloadProgress(
        task_id=task["task_id"],
        status=task["status"],
        phase=task["phase"] or task["status"],
        symbols_total=len(set(k.split("/")[0] for k in json.loads(task.get("stats_json", "{}")).keys())) if task.get("stats_json") else 0,
        symbols_done=0,
        timeframes_total=len(set(k.split("/")[1] for k in json.loads(task.get("stats_json", "{}")).keys())) if task.get("stats_json") else 0,
        timeframes_done=0,
        downloaded_bytes=task.get("downloaded_bytes", 0),
        processed_candles=task.get("processed_candles", 0),
        gaps_found=task.get("gaps_found", 0),
        gaps_filled_public=task.get("gaps_filled_public", 0),
        gaps_filled_rest=task.get("gaps_filled_rest", 0),
        errors=json.loads(task.get("errors_json", "[]")) if task.get("errors_json") else [],
        current_symbol=task.get("current_symbol"),
        current_timeframe=task.get("current_timeframe"),
    )


@router.get("/plan/{task_id}", response_model=DownloadPlan)
async def plan_endpoint(
    task_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
) -> DownloadPlan:
    """Plan de descarga generado (para debug/inspección)."""
    import tempfile
    from pathlib import Path
    plan_file = Path(tempfile.gettempdir()) / f"download_plan_{task_id}.json"
    if not plan_file.exists():
        raise HTTPException(status_code=404, detail="Plan not found")
    return DownloadPlan.model_validate_json(plan_file.read_text())


@router.get("/result/{task_id}", response_model=DownloadResult)
async def result_endpoint(
    task_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
) -> DownloadResult:
    """Resultado final con rutas Parquet y stats."""
    task_manager = SQLTaskManager(settings.TASKS_DB_PATH)
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task["status"] not in ("COMPLETED", "FAILED"):
        raise HTTPException(status_code=409, detail=f"Task not finished: {task['status']}")

    return DownloadResult(
        task_id=task["task_id"],
        status=task["status"],
        parquet_paths=json.loads(task.get("parquet_paths_json", "[]")),
        stats=json.loads(task.get("stats_json", "{}")),
        gaps_unfillable=[],  # TODO: persistir en task manager
    )
