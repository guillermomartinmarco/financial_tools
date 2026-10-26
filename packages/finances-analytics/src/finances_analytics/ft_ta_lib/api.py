"""Router FastAPI ``/ta-lib`` de ``ft_ta_lib`` (plan §2.2); lo monta ``finances-api``.

- ``POST /compute`` valida (400; incluida la ejecución de prueba de RF-4 sobre una serie
  sintética), comprueba existencia (404) y bloqueos (409) en el *threadpool*, registra la
  tarea ``PENDING`` y responde ``202`` de inmediato; el cálculo corre en ``BackgroundTasks``
  dentro del *threadpool* con su propio event loop (Art. 3, Art. 18).
- Lecturas de SQLite/disco y la exportación se delegan a ``run_in_threadpool``; la
  exportación se genera en un directorio temporal y se sirve con ``StreamingResponse``.
- Los errores de validación de la petición (cuerpo, ruta o *query*) responden ``400``
  (RF-12), no el ``422`` por defecto de FastAPI.
- Todas las rutas se llaman ``ta_lib_<acción>``: los nombres son únicos en la app compuesta
  (``ft_serve``) y ``url_for`` resuelve las URLs de *polling* hacia ``/ta-lib``.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, TypeVar

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, status
from fastapi import Path as PathParam
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from finances_core.config import ConfigError, FinancesSettings
from finances_core.export import MEDIA_TYPES, ForbiddenOutputDirError
from finances_core.indicators_store.manifest import DatasetLockedError
from finances_core.indicators_store.source import InvalidSourceError, SourceNotFoundError
from finances_core.indicators_store.store import SeriesNotFoundError
from finances_core.indicators_store.view import ExportValidationError
from finances_core.marketdata.tasks_lifecycle import TaskNotFinishedError
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    ComputeProgress,
    ComputeResult,
    DispatchResponse,
    SeriesExportRequest,
    StoreManifest,
)
from finances_core.schemas.ta_lib import TaLibCatalog, TaLibComputeRequest, TaLibPlan
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import service
from .errors import EmptyExportError, TaLibValidationError
from .exporter import export_file_name

TASK_ID_PATTERN = r"^[0-9a-f-]{36}$"
STALE_HEADER = "X-FT-Indicators-Stale"
_CHUNK_SIZE = 1 << 20

#: Nombres de ruta únicos en la app compuesta (``url_for`` resuelve por nombre).
CATALOG_ROUTE = "ta_lib_catalog"
COMPUTE_ROUTE = "ta_lib_compute"
STATUS_ROUTE = "ta_lib_status"
PLAN_ROUTE = "ta_lib_plan"
RESULT_ROUTE = "ta_lib_result"
MANIFEST_ROUTE = "ta_lib_manifest"
EXPORT_ROUTE = "ta_lib_export"

T = TypeVar("T")
Handler = Callable[[Request], Coroutine[Any, Any, Response]]


class BadRequestRoute(APIRoute):
    """Ruta que traduce ``RequestValidationError`` (cuerpo/ruta/*query*) a HTTP 400."""

    def get_route_handler(self) -> Handler:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=jsonable_encoder(exc.errors()),
                ) from exc

        return handler


router = APIRouter(prefix=service.API_PREFIX, tags=["ft_ta_lib"], route_class=BadRequestRoute)


def _http_error(exc: Exception) -> HTTPException:
    """Errores de dominio → códigos HTTP de RF-12/RF-13."""
    if isinstance(exc, DatasetLockedError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, SourceNotFoundError | SeriesNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, ConfigError | InvalidSourceError):
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    elif isinstance(exc, EmptyExportError | ValueError):  # validación (incl. pydantic, TA-Lib)
        code = status.HTTP_400_BAD_REQUEST
    else:  # OSError (destino no escribible) u otro
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    return HTTPException(status_code=code, detail=str(exc))


_DOMAIN_ERRORS = (
    TaLibValidationError,
    DatasetLockedError,
    SourceNotFoundError,
    SeriesNotFoundError,
    InvalidSourceError,
    ConfigError,
    ExportValidationError,
    ForbiddenOutputDirError,
    EmptyExportError,
    ValueError,
    OSError,
)


async def _run(fn: Callable[[], T]) -> T:
    """Ejecuta ``fn`` en el *threadpool* traduciendo errores de dominio a HTTP."""
    try:
        return await run_in_threadpool(fn)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


def _dataset(exchange: str, market_type: str, symbol: str, timeframe: str) -> DatasetIdentifier:
    try:
        return DatasetIdentifier.model_validate(
            {
                "exchange": exchange,
                "market_type": market_type,
                "symbol": symbol,
                "timeframe": timeframe,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ---------------------------------------------------------------- endpoints


@router.get("/catalog", response_model=TaLibCatalog, name=CATALOG_ROUTE)
async def catalog_endpoint(
    group: str | None = Query(None, description="Grupo de TA-Lib (p. ej. Pattern Recognition)"),
    function: str | None = Query(None, description="Función de TA-Lib (p. ej. RSI)"),
) -> TaLibCatalog:
    """Catálogo dinámico de TA-Lib (RF-2), filtrable por grupo y/o función (400 si no existen).

    La primera llamada construye el catálogo (ejecución de prueba de cada función) en el
    *threadpool*; después está en caché.
    """
    return await _run(lambda: service.catalog(group, function))


@router.post(
    "/compute",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DispatchResponse,
    name=COMPUTE_ROUTE,
)
async def compute_endpoint(
    body: TaLibComputeRequest, request: Request, background_tasks: BackgroundTasks
) -> DispatchResponse:
    """Despacha el cálculo y retorna ``task_id`` + URLs de *polling* sin esperar (Art. 18)."""
    settings = FinancesSettings()
    prepared = await _run(lambda: service.prepare_compute(body, settings))
    await _run(lambda: service.create_task(prepared, settings))
    background_tasks.add_task(service.run_compute_sync, prepared, settings)
    task_id = prepared.task_id
    return DispatchResponse(
        task_id=task_id,
        status="ACCEPTED",
        poll_url=str(request.url_for(STATUS_ROUTE, task_id=task_id)),
        plan_url=str(request.url_for(PLAN_ROUTE, task_id=task_id)),
        result_url=str(request.url_for(RESULT_ROUTE, task_id=task_id)),
    )


@router.get("/status/{task_id}", response_model=ComputeProgress, name=STATUS_ROUTE)
async def status_endpoint(
    task_id: str = PathParam(..., pattern=TASK_ID_PATTERN),
) -> ComputeProgress:
    """Progreso por fase con ``stability`` de los items terminados (RF-11, RF-17)."""
    progress = await _run(lambda: service.get_progress(task_id))
    if progress is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return progress


@router.get("/plan/{task_id}", response_model=TaLibPlan, name=PLAN_ROUTE)
async def plan_endpoint(task_id: str = PathParam(..., pattern=TASK_ID_PATTERN)) -> TaLibPlan:
    """Plan persistido (``share/plans/ta_lib_plan_{task_id}.json``)."""
    plan = await _run(lambda: service.get_plan(task_id))
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return plan


@router.get("/result/{task_id}", response_model=ComputeResult, name=RESULT_ROUTE)
async def result_endpoint(task_id: str = PathParam(..., pattern=TASK_ID_PATTERN)) -> ComputeResult:
    """Resultado final (``409`` mientras la tarea no termina)."""
    try:
        result = await run_in_threadpool(service.get_result, task_id)
    except TaskNotFinishedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Task not finished: {exc.status}"
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return result


@router.get(
    "/datasets/{exchange}/{market_type}/{symbol}/{timeframe}",
    response_model=StoreManifest,
    name=MANIFEST_ROUTE,
)
async def manifest_endpoint(
    exchange: str, market_type: str, symbol: str, timeframe: str
) -> StoreManifest:
    """Manifiesto ``_manifest.ft_ta_lib.json`` del dataset (RF-8)."""
    ds = _dataset(exchange, market_type, symbol, timeframe)
    manifest = await _run(lambda: service.get_manifest(ds))
    if manifest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sin funciones de TA-Lib calculadas para {ds.path_segment()}",
        )
    return manifest


def _iter_file(path: Path) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            yield chunk


def _export_to_file(
    ds: DatasetIdentifier, req: SeriesExportRequest
) -> tuple[service.ExportOutcome, Path, Path | None]:
    """Exporta al ``output_dir`` pedido o a un temporal (devuelto para limpieza)."""
    if req.output_dir and req.output_dir != "-":
        outcome = service.export(ds, req)
        tmp_dir = None
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ft_ta_lib_export_"))
        try:
            outcome = service.export(ds, req.model_copy(update={"output_dir": str(tmp_dir)}))
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
    if outcome.path is None:  # inalcanzable: export a directorio siempre devuelve ruta
        msg = "La exportación no devolvió ruta de salida"
        raise RuntimeError(msg)
    return outcome, outcome.path, tmp_dir


@router.post("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export", name=EXPORT_ROUTE)
async def export_endpoint(
    body: SeriesExportRequest, exchange: str, market_type: str, symbol: str, timeframe: str
) -> StreamingResponse:
    """Exporta la tabla ancha ``talib_*`` (RF-10) como descarga *streaming*; no recalcula."""
    ds = _dataset(exchange, market_type, symbol, timeframe)
    outcome, path, tmp_dir = await _run(lambda: _export_to_file(ds, body))
    headers = {"Content-Disposition": f'attachment; filename="{export_file_name(ds, body.format)}"'}
    if outcome.stale:
        headers[STALE_HEADER] = ",".join(outcome.stale)
    cleanup = BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True) if tmp_dir else None
    return StreamingResponse(
        _iter_file(path),
        media_type=MEDIA_TYPES[body.format],
        headers=headers,
        background=cleanup,
    )


__all__ = ["STALE_HEADER", "TASK_ID_PATTERN", "BadRequestRoute", "router"]
