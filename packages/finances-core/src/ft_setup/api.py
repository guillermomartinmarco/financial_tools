"""Router FastAPI de ``ft_setup`` (RF-5) — adaptador delgado sobre ``service``."""

from __future__ import annotations

import logging

import structlog
from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Path, Request, status
from fastapi.concurrency import run_in_threadpool

from finances_core.schemas.setup import InitTaskResult, SetupDoctorResponse
from ft_setup.logging import configure_logging
from ft_setup.service import diagnose, get_init_result, run_init, start_init_task

router = APIRouter(prefix="/setup", tags=["setup"])
logger = structlog.get_logger(__name__)

TASK_ID_PATTERN = r"^[0-9a-f-]{36}$"


@router.get("/doctor", response_model=SetupDoctorResponse)
async def doctor_endpoint() -> SetupDoctorResponse:
    """RF-2: diagnóstico completo de variables (mismo dato que el CLI)."""
    return await run_in_threadpool(diagnose)


@router.post("/init", status_code=status.HTTP_202_ACCEPTED)
async def init_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, str]:
    """RF-5: inicializa el entorno en background y devuelve ``task_id`` + ``poll_url``."""
    started = await run_in_threadpool(start_init_task, force, dry_run)

    def _run() -> None:
        structlog.contextvars.bind_contextvars(component="ft_setup", task_id=started.task_id)
        try:
            run_init(force=force, dry_run=dry_run, task_id=started.task_id)
        except Exception as exc:  # noqa: BLE001 - el estado FAILED ya queda persistido
            logger.error(  # noqa: TRY400 - structlog, no stdlib logging
                "init_background_failed", task_id=started.task_id, error=str(exc)
            )

    background_tasks.add_task(_run)

    logger.info("init_dispatched", task_id=started.task_id, force=force, dry_run=dry_run)
    return {
        "task_id": started.task_id,
        "status": "ACCEPTED",
        "poll_url": str(request.url_for("init_status", task_id=started.task_id)),
    }


@router.get("/status/{task_id}", response_model=InitTaskResult)
async def init_status(task_id: str = Path(..., pattern=TASK_ID_PATTERN)) -> InitTaskResult:
    """RF-5: polling del estado de la tarea de inicialización."""
    result = await run_in_threadpool(get_init_result, task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


def configure_api_logging(level: int = logging.INFO) -> None:
    """Configura logging JSON (usado por el entrypoint del servicio)."""
    configure_logging("api", level)


def create_app() -> FastAPI:
    """Crea una app FastAPI mínima con el router de ``ft_setup``."""
    app = FastAPI(title="ft_setup", version="0.1.0")
    app.include_router(router)
    return app
