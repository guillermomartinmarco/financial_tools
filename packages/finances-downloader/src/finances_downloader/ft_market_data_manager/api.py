"""Router FastAPI ``/market-data/manager`` (lo monta ``finances-api``; aquí no hay app).

- ``POST /compact|/dedupe|/prune|/import`` validan de forma síncrona, registran la tarea ``PENDING``
  en ``SQLTaskManager`` y responden ``202`` de inmediato; el trabajo Polars/Parquet corre en
  ``BackgroundTasks`` (función síncrona => *threadpool*, nunca en el event loop; Art. 3, 18).
- Los ``GET`` delegan las lecturas SQLite en ``run_in_threadpool``.
- ``FAILED`` solo ante error de IO o excepción inesperada: una partición en ``error`` o
  ``conflict`` queda en el resultado y no hace fallar la tarea (RF-6, RF-7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Request, status
from finances_core.config import FinancesSettings
from finances_core.marketdata.plans import new_task_id
from finances_core.marketdata.tasks_lifecycle import (
    TaskNotFinishedError,
    create_download_task,
    ensure_finished,
    json_field,
    load_task_row,
)
from finances_core.schemas.market_data_manager import (
    ImportRequest,
    ManagerRequest,
    ManagerResult,
    ManagerTaskResponse,
    PartitionResult,
    PruneRequest,
)
from finances_core.tasks import SQLTaskManager
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from finances_downloader.ft_market_data_manager import importer, service
from finances_downloader.ft_market_data_manager.errors import (
    ConflictError,
    DatasetNotFoundError,
    ImportValidationError,
    InvalidArgumentError,
)

router = APIRouter(prefix="/market-data/manager", tags=["market-data-manager"])
logger = structlog.get_logger(__name__, component="ft_market_data_manager")

TASK_ID_PATTERN = r"^[0-9a-f-]{36}$"
TaskId = Annotated[str, Path(pattern=TASK_ID_PATTERN)]
Action = Literal["compact", "dedupe", "prune", "import"]
AnyRequest = ManagerRequest | ImportRequest

#: Estado HTTP de ``GET /result`` para una tarea ``FAILED`` por un error tipado de negocio
#: (``import`` valida el archivo dentro de la tarea; RF-10). Cualquier otra causa => 500.
HTTP_422_UNPROCESSABLE = 422  # nombre de la constante de Starlette cambia entre versiones
FAILURE_STATUS = {
    "validation": HTTP_422_UNPROCESSABLE,
    "conflict": status.HTTP_409_CONFLICT,
    "not_found": status.HTTP_404_NOT_FOUND,
}


class TaskFailedError(RuntimeError):
    """La tarea terminó en ``FAILED``; ``kind`` clasifica la causa (``unexpected`` por defecto)."""

    def __init__(self, message: str, kind: str = "unexpected") -> None:
        super().__init__(message)
        self.kind = kind


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, InvalidArgumentError | ImportValidationError):
        return "validation"
    if isinstance(exc, ConflictError):
        return "conflict"
    if isinstance(exc, DatasetNotFoundError):
        return "not_found"
    return "unexpected"


class ManagerProgress(BaseModel):
    """Respuesta de ``GET /status/{task_id}`` (RF-7)."""

    task_id: str
    status: str
    partitions_total: int = 0
    partitions_done: int = 0
    partitions_changed: int = 0
    partitions_conflict: int = 0
    rows_affected: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    errors_json: list[str] = []


# --- Tarea en segundo plano -----------------------------------------------------------------


@dataclass
class _Counters:
    """Contadores acumulados de una tarea; se persisten en ``stats_json`` por partición.

    ``partitions_changed``/``rows_affected`` cuentan las particiones modificadas (o, con
    ``dry_run``, las que se modificarían: ``planned``).
    """

    partitions_total: int = 0
    partitions_done: int = 0
    partitions_changed: int = 0
    partitions_conflict: int = 0
    rows_affected: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, total: int, done: int, result: PartitionResult) -> None:
        self.partitions_total = total
        self.partitions_done = done
        self.bytes_before += result.bytes_before
        self.bytes_after += result.bytes_after
        if result.result in {"changed", "planned"}:
            self.partitions_changed += 1
            self.rows_affected += result.rows_before
        elif result.result == "conflict":
            self.partitions_conflict += 1
        if result.result in {"error", "conflict"}:
            period = f"{result.year:04d}-{result.month:02d}"
            self.errors.append(f"{result.dataset} {period}: {result.detail or result.result}")

    def stats(self) -> dict[str, int]:
        return {k: v for k, v in self.__dict__.items() if isinstance(v, int)}


def _runner(action: Action) -> Callable[[Any, service.PartitionCallback], ManagerResult]:
    """Función del servicio para ``action``, resuelta al llamar (no al importar)."""
    if action == "compact":
        return service.run_compact
    if action == "dedupe":
        return service.run_dedupe
    if action == "import":
        return service.run_import
    return service.run_prune


def run_manager_task(action: Action, request: AnyRequest, task_id: str) -> None:
    """Entrada para ``BackgroundTasks``: ejecuta el servicio y persiste progreso y resultado.

    Nunca propaga excepciones: el fallo (IO o inesperado) queda registrado como ``FAILED``.
    """
    manager = SQLTaskManager(FinancesSettings().TASKS_DB_PATH)
    counters = _Counters()

    def on_partition(done: int, total: int, result: PartitionResult) -> None:
        counters.add(total, done, result)
        manager.update_task(task_id, stats_json=counters.stats(), errors_json=counters.errors)

    try:
        manager.update_task(task_id, status="RUNNING", phase="RUNNING")
        result = _runner(action)(request, on_partition)
        manager.update_task(
            task_id,
            status="COMPLETED",
            phase="COMPLETED",
            stats_json=counters.stats(),
            errors_json=counters.errors,
            result_summary_json={"action": action, "result": result.model_dump(mode="json")},
        )
    except Exception as exc:
        logger.exception("manager_task_failed", task_id=task_id, action=action, error=str(exc))
        counters.errors.append(f"{type(exc).__name__}: {exc}")
        manager.update_task(
            task_id,
            status="FAILED",
            phase="FAILED",
            error=str(exc),
            stats_json=counters.stats(),
            errors_json=counters.errors,
            result_summary_json={"error_kind": _error_kind(exc)},
        )


# --- Consultas ------------------------------------------------------------------------------


def get_progress(task_id: str) -> ManagerProgress | None:
    row = load_task_row(task_id)
    if row is None:
        return None
    stats: dict[str, int] = json_field(row, "stats_json", {})
    return ManagerProgress(
        task_id=row["task_id"],
        status=str(row["status"]),
        errors_json=json_field(row, "errors_json", []),
        **stats,
    )


def get_result(task_id: str) -> ManagerResult | None:
    """Resultado final o ``None`` si la tarea no existe.

    Raises:
        TaskNotFinishedError: la tarea aún no está en ``COMPLETED``/``FAILED``.
        TaskFailedError: la tarea terminó en ``FAILED`` (no hay resultado).
    """
    row = load_task_row(task_id)
    if row is None:
        return None
    if ensure_finished(task_id, row) == "FAILED":
        msg = str(row.get("error") or "la tarea falló")
        kind = json_field(row, "result_summary_json", {}).get("error_kind", "unexpected")
        raise TaskFailedError(msg, kind)
    summary: dict[str, Any] = json_field(row, "result_summary_json", {})
    return ManagerResult.model_validate(summary["result"])


# --- Despacho -------------------------------------------------------------------------------


async def _enqueue(
    action: Action, request: AnyRequest, http_request: Request, tasks: BackgroundTasks
) -> ManagerTaskResponse:
    task_id = new_task_id()
    summary = {"action": action, "request": request.model_dump(mode="json")}
    await run_in_threadpool(create_download_task, task_id, summary)
    tasks.add_task(run_manager_task, action, request, task_id)
    return ManagerTaskResponse(
        task_id=task_id,
        poll_url=str(http_request.url_for("manager_status", task_id=task_id)),
    )


async def _dispatch(
    action: Action, request: ManagerRequest, http_request: Request, tasks: BackgroundTasks
) -> ManagerTaskResponse:
    try:
        service.validate_selector(request)
    except InvalidArgumentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return await _enqueue(action, request, http_request, tasks)


@router.post(
    "/compact",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ManagerTaskResponse,
    name="manager_compact",
)
async def compact_endpoint(
    body: ManagerRequest, request: Request, tasks: BackgroundTasks
) -> ManagerTaskResponse:
    """RF-1: compacta las particiones seleccionadas (``dry_run`` devuelve el plan)."""
    return await _dispatch("compact", body, request, tasks)


@router.post(
    "/dedupe",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ManagerTaskResponse,
    name="manager_dedupe",
)
async def dedupe_endpoint(
    body: ManagerRequest, request: Request, tasks: BackgroundTasks
) -> ManagerTaskResponse:
    """RF-2: elimina duplicados conservando la última fila en orden de lectura."""
    return await _dispatch("dedupe", body, request, tasks)


@router.post(
    "/prune",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ManagerTaskResponse,
    name="manager_prune",
)
async def prune_endpoint(
    body: PruneRequest, request: Request, tasks: BackgroundTasks
) -> ManagerTaskResponse:
    """RF-3: borra particiones completas; 400 sin ``confirm`` y sin ``dry_run``."""
    if not (body.confirm or body.dry_run):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prune exige confirm=true salvo con dry_run",
        )
    return await _dispatch("prune", body, request, tasks)


@router.post(
    "/import",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ManagerTaskResponse,
    name="manager_import",
)
async def import_endpoint(
    body: ImportRequest, request: Request, tasks: BackgroundTasks
) -> ManagerTaskResponse:
    """RF-4/RF-5: importa un archivo; 400 sin ``confirm`` y sin ``dry_run``.

    Antes de crear la tarea valida de forma síncrona el origen (existencia y ubicación fuera
    de ``FT_PARQUET_DIR``), el formato y el destino (404 si no existe y no hay ``create``).
    El contenido del archivo se valida dentro de la tarea: un archivo inválido deja la tarea
    en ``FAILED`` y ``GET /result`` responde 422 (409 por conflicto) sin escribir nada.
    """
    if not (body.confirm or body.dry_run):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="import exige confirm=true salvo con dry_run",
        )
    try:
        await run_in_threadpool(importer.resolve_target, body, FinancesSettings().PARQUET_DIR)
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidArgumentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return await _enqueue("import", body, request, tasks)


@router.get("/status/{task_id}", response_model=ManagerProgress, name="manager_status")
async def status_endpoint(task_id: TaskId) -> ManagerProgress:
    """Estado (``PENDING`` -> ``RUNNING`` -> ``COMPLETED``/``FAILED``) y contadores (RF-7)."""
    progress = await run_in_threadpool(get_progress, task_id)
    if progress is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return progress


@router.get("/result/{task_id}", response_model=ManagerResult, name="manager_result")
async def result_endpoint(task_id: TaskId) -> ManagerResult:
    """Resultado por partición; 409 si la tarea no terminó, 500 si terminó en ``FAILED``."""
    try:
        result = await run_in_threadpool(get_result, task_id)
    except TaskNotFinishedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Task not finished: {exc.status}"
        ) from exc
    except TaskFailedError as exc:
        code = FAILURE_STATUS.get(exc.kind, status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(status_code=code, detail=f"Task failed: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return result
