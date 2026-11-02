"""Router FastAPI de ``ft_strategy_manager`` (RF-10): adaptador delgado sobre ``service``.

Cada endpoint delega en ``service`` mediante ``run_in_threadpool`` (Art. 3: SQLite es
bloqueante) y traduce los errores de negocio a HTTP: 404 inexistente, 409 nombre duplicado,
422 parámetros/schema/patch inválidos y 400 "No changes detected".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, ParamSpec, TypeVar

import structlog
from fastapi import APIRouter, Header, HTTPException, Path, Query, Response, status
from fastapi.concurrency import run_in_threadpool

from finances_core.schemas.strategy import (
    DiffResult,
    Strategy,
    StrategyCreate,
    StrategyListFilters,
    StrategyListResponse,
    StrategyUpdate,
    StrategyVersion,
)
from ft_strategy_manager import service
from ft_strategy_manager.errors import (
    NoChangesError,
    StrategyConflictError,
    StrategyNotFoundError,
    StrategyValidationError,
    VersionNotFoundError,
)

router = APIRouter(prefix="/strategies", tags=["strategies"])
logger = structlog.get_logger(__name__)

STRATEGY_ID_PATTERN = r"^[0-9a-f-]{36}$"
StrategyId = Annotated[str, Path(pattern=STRATEGY_ID_PATTERN)]
UserHeader = Annotated[str | None, Header(alias="X-User-ID")]

P = ParamSpec("P")
T = TypeVar("T")

EXPORT_MEDIA_TYPES = {"json": "application/json", "yaml": "application/yaml"}


async def _call(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Ejecuta ``func`` en el threadpool y traduce los errores de negocio a ``HTTPException``."""
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    except (StrategyNotFoundError, VersionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except StrategyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except StrategyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NoChangesError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("", status_code=status.HTTP_201_CREATED, response_model=StrategyVersion)
async def strategies_create(
    payload: StrategyCreate, response: Response, x_user_id: UserHeader = None
) -> StrategyVersion:
    """RF-1: crea la estrategia v1. Devuelve la versión creada y el header ``Location``."""
    created = await _call(service.create_strategy, payload, user=x_user_id)
    response.headers["Location"] = f"{router.prefix}/{created.strategy_id}"
    return created


@router.get("", response_model=StrategyListResponse)
async def strategies_list(
    tag: Annotated[str | None, Query(description="Tag exacto")] = None,
    name_contains: Annotated[
        str | None, Query(alias="name", description="Substring case-insensitive")
    ] = None,
    include_deleted: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StrategyListResponse:
    """RF-5: lista estrategias con filtros y paginación."""
    filters = StrategyListFilters(
        tag=tag,
        name_contains=name_contains,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return await _call(service.list_strategies, filters)


@router.get("/{strategy_id}", response_model=Strategy)
async def strategies_get(
    strategy_id: StrategyId, version: Annotated[int | None, Query(ge=1)] = None
) -> Strategy:
    """RF-2: estrategia completa; ``version`` opcional (por defecto, la última)."""
    return await _call(service.get_strategy, strategy_id, version)


@router.put("/{strategy_id}", response_model=StrategyVersion)
async def strategies_update(
    strategy_id: StrategyId, payload: StrategyUpdate, x_user_id: UserHeader = None
) -> StrategyVersion:
    """RF-3: crea una nueva versión (reemplazo completo o JSON Patch)."""
    return await _call(service.update_strategy, strategy_id, payload, user=x_user_id)


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def strategies_delete(
    strategy_id: StrategyId, purge: bool = False, x_user_id: UserHeader = None
) -> Response:
    """RF-4: soft-delete (o hard-delete con ``purge=true``); idempotente."""
    await _call(service.delete_strategy, strategy_id, purge=purge, user=x_user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{strategy_id}/versions", response_model=list[StrategyVersion])
async def strategies_versions(
    strategy_id: StrategyId,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[StrategyVersion]:
    """RF-10: versiones paginadas en orden ascendente."""
    return await _call(service.list_versions, strategy_id, limit, offset)


@router.get("/{strategy_id}/diff", response_model=DiffResult)
async def strategies_diff(
    strategy_id: StrategyId,
    from_version: Annotated[int, Query(ge=1, alias="from")],
    to_version: Annotated[int, Query(ge=1, alias="to")],
) -> DiffResult:
    """RF-6: diff semántico entre dos versiones."""
    return await _call(service.diff_versions, strategy_id, from_version, to_version)


@router.get("/{strategy_id}/export")
async def strategies_export(
    strategy_id: StrategyId, format: Literal["json", "yaml"] = "json"
) -> Response:
    """RF-10/ADR-S08: descarga la estrategia con todas sus versiones (JSON o YAML)."""
    content = await _call(service.export_strategy, strategy_id, format)
    return Response(
        content=content,
        media_type=EXPORT_MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="strategy-{strategy_id}.{format}"'},
    )
