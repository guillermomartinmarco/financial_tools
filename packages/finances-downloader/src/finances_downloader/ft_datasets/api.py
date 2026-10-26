"""Router FastAPI de ft_datasets (adaptador delgado, Art. 1; rutas en plan §2.2).

Se exporta ``router`` (prefijo ``/datasets``); lo monta el paquete ``finances-api``.

- ``POST /materialize/{name}`` valida (sin red), registra la tarea ``PENDING`` y responde
  ``202`` de inmediato; la tarea corre en ``BackgroundTasks`` (hilo con su event loop).
- ``verify``/``lock`` y toda lectura de disco/SQLite corren en ``run_in_threadpool``
  (Art. 3). ``name`` se valida con la expresión de RF-1 (``422`` si no cumple) y se
  resuelve solo dentro de ``FT_DATASETS_RECIPES_DIR``.
- Nombres de ruta únicos ``datasets_*`` (la app compuesta resuelve ``url_for`` por nombre).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn, TypeVar

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from fastapi import status as http
from fastapi.responses import JSONResponse
from finances_core.schemas.datasets import (
    RECIPE_NAME_PATTERN,
    LockResult,
    MaterializePlan,
    MaterializeProgress,
    MaterializeResult,
    VerifyReport,
)
from finances_core.schemas.dispatch import DispatchResponse
from starlette.concurrency import run_in_threadpool

from finances_downloader.ft_datasets import service
from finances_downloader.ft_datasets.errors import DOMAIN_ERRORS, http_status_for
from finances_downloader.ft_datasets.runner import Runner, SubprocessRunner

router = APIRouter(prefix="/datasets", tags=["datasets"])

TASK_ID_PATTERN = r"^[0-9a-f-]{36}$"
STATUS_ROUTE = "datasets_status"
PLAN_ROUTE = "datasets_plan"
RESULT_ROUTE = "datasets_result"

T = TypeVar("T")


def get_runner() -> Runner:
    """Dependencia del *runner* de descargas (los tests la sustituyen)."""
    return SubprocessRunner()


def _raise(exc: Exception) -> NoReturn:
    raise HTTPException(status_code=http_status_for(exc), detail=str(exc)) from exc


async def _run(fn: Callable[[], T]) -> T:
    """Ejecuta ``fn`` en el *threadpool* traduciendo errores de dominio a HTTP (RF-12)."""
    try:
        return await run_in_threadpool(fn)
    except DOMAIN_ERRORS as exc:
        _raise(exc)


@router.get("/recipes", response_model=list[service.RecipeSummary], name="datasets_recipes")
async def recipes_endpoint() -> list[service.RecipeSummary]:
    """Recetas y estado de su *lock* (sin verificar datos)."""
    return await _run(service.list_recipes)


@router.get("/recipes/{name}", response_model=service.RecipeDetail, name="datasets_recipe")
async def recipe_endpoint(
    name: str = Path(..., pattern=RECIPE_NAME_PATTERN),
) -> service.RecipeDetail:
    """Receta expandida, claves de ítem y *lock* si existe."""
    return await _run(lambda: service.show_recipe(name))


@router.post(
    "/materialize/{name}",
    status_code=http.HTTP_202_ACCEPTED,
    response_model=DispatchResponse,
    name="datasets_materialize",
)
async def materialize_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Path(..., pattern=RECIPE_NAME_PATTERN),
    verify: bool = Query(True, description="Verificar al terminar"),
    refetch: bool = Query(False, description="Re-descargar ítems en MISMATCH"),
    runner: Runner = Depends(get_runner),
) -> DispatchResponse:
    """Despacha la materialización y retorna ``task_id`` + URLs de *polling* sin esperar."""
    await _run(lambda: service.prepare(name))
    task_id = service.new_task_id()
    await run_in_threadpool(
        lambda: service.create_materialize_task(task_id, name, verify=verify, refetch=refetch)
    )
    background_tasks.add_task(
        service.run_materialize_sync,
        name,
        task_id,
        verify=verify,
        refetch=refetch,
        runner=runner,
    )
    return DispatchResponse(
        task_id=task_id,
        poll_url=str(request.url_for(STATUS_ROUTE, task_id=task_id)),
        plan_url=str(request.url_for(PLAN_ROUTE, task_id=task_id)),
        result_url=str(request.url_for(RESULT_ROUTE, task_id=task_id)),
    )


@router.get("/status/{task_id}", response_model=MaterializeProgress, name=STATUS_ROUTE)
async def status_endpoint(
    task_id: str = Path(..., pattern=TASK_ID_PATTERN),
) -> MaterializeProgress:
    """Progreso por fase (RF-8)."""
    progress = await run_in_threadpool(service.get_progress, task_id)
    if progress is None:
        raise HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail="Task not found")
    return progress


@router.get("/plan/{task_id}", response_model=MaterializePlan, name=PLAN_ROUTE)
async def plan_endpoint(task_id: str = Path(..., pattern=TASK_ID_PATTERN)) -> MaterializePlan:
    """Plan persistido (ítems omitidos, ``refetch`` y comandos exactos)."""
    plan = await run_in_threadpool(service.get_plan, task_id)
    if plan is None:
        raise HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail="Plan not found")
    return plan


@router.get("/result/{task_id}", response_model=MaterializeResult, name=RESULT_ROUTE)
async def result_endpoint(task_id: str = Path(..., pattern=TASK_ID_PATTERN)) -> MaterializeResult:
    """Resultado final: bloques ejecutados, verificación y errores (``409`` si no terminó)."""
    try:
        result = await run_in_threadpool(service.get_result, task_id)
    except service.TaskNotFinishedError as exc:
        raise HTTPException(
            status_code=http.HTTP_409_CONFLICT, detail=f"Task not finished: {exc.status}"
        ) from exc
    if result is None:
        raise HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail="Task not found")
    return result


@router.post("/verify/{name}", response_model=VerifyReport, name="datasets_verify")
async def verify_endpoint(name: str = Path(..., pattern=RECIPE_NAME_PATTERN)) -> VerifyReport:
    """Verificación local (``200`` con el estado en el cuerpo; ``409`` si los datos no son
    canónicos)."""
    return await _run(lambda: service.verify(name))


@router.post(
    "/lock/{name}",
    response_model=LockResult,
    name="datasets_lock",
    responses={http.HTTP_409_CONFLICT: {"model": LockResult}},
)
async def lock_endpoint(
    name: str = Path(..., pattern=RECIPE_NAME_PATTERN),
    update: bool = Query(False, description="Reescribir un lock que difiere"),
) -> LockResult | JSONResponse:
    """Bloqueo explícito (RF-7); ``409`` con el ``LockResult`` si se rechaza."""
    result = await _run(lambda: service.lock_recipe(name, update=update))
    if result.outcome == "REJECTED":
        return JSONResponse(
            status_code=http.HTTP_409_CONFLICT, content=result.model_dump(mode="json")
        )
    return result
