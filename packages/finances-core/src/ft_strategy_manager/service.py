"""Lógica de negocio compartida por CLI y API (Art. 1): CRUD versionado, diff y export.

Todas las funciones son síncronas y abren su propia conexión (ver ``db``): la API las
despacha con ``run_in_threadpool`` (Art. 3). ``db_path`` es opcional y solo existe para
inyectar una base concreta; por defecto se resuelve con ``db.resolve_db_path``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Literal

import structlog
import uuid6
import yaml

from finances_core.schemas.strategy import (
    DiffResult,
    Strategy,
    StrategyCreate,
    StrategyListFilters,
    StrategyListResponse,
    StrategyUpdate,
    StrategyVersion,
)
from finances_core.tasks import now_ms
from ft_strategy_manager import db
from ft_strategy_manager.errors import (
    NoChangesError,
    StrategyConflictError,
    StrategyNotFoundError,
    StrategyValidationError,
    VersionNotFoundError,
)
from ft_strategy_manager.utils import (
    apply_json_patch,
    check_parameter_schema,
    compute_diff,
    compute_hash,
    mask_secrets,
    validate_parameters,
)

logger = structlog.get_logger(__name__)

ExportFormat = Literal["json", "yaml"]

_init_lock = threading.Lock()
_initialised: set[Path] = set()


def _ready_db(db_path: Path | None) -> Path:
    """Ruta de la base con el esquema creado (idempotente, se recrea si el archivo no existe)."""
    path = Path(db_path) if db_path is not None else db.resolve_db_path()
    with _init_lock:
        if path not in _initialised or not path.exists():
            db.init_db(path)
            _initialised.add(path)
    return path


def log_audit(  # noqa: PLR0913, PLR0917 - campos de auditoría exigidos por RF-9
    action: str,
    strategy_id: str,
    version: int,
    hash_before: str | None,
    hash_after: str | None,
    diff_summary: dict[str, Any],
    user: str | None = None,
) -> None:
    """RF-9: evento de auditoría INFO para toda operación mutante.

    ``diff_summary`` se enmascara (Art. 9): los valores de claves sensibles
    (``*password*``, ``*token*``, ``*secret*``, ``*api_key*``...) nunca llegan al log.
    """
    logger.info(
        "strategy_audit",
        action=action,
        strategy_id=strategy_id,
        version=version,
        hash_before=hash_before,
        hash_after=hash_after,
        diff_summary=mask_secrets(diff_summary),
        user=user,
    )


def _diff_summary(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    diff = compute_diff(before, after)
    return {"added": diff.added, "removed": diff.removed, "changed": diff.changed}


def _validate_or_raise(parameters: dict[str, Any], schema: dict[str, Any] | None) -> None:
    ok, detail = validate_parameters(parameters, schema)
    if not ok:
        msg = f"Parameters do not match parameter_schema: {detail}"
        raise StrategyValidationError(msg)


# --------------------------------------------------------------------------- create
def create_strategy(
    payload: StrategyCreate, user: str | None = None, db_path: Path | None = None
) -> StrategyVersion:
    """RF-1/RF-7: crea la estrategia y su versión 1 de forma atómica.

    Raises:
        StrategyConflictError: SI ``name`` ya existe (incluye estrategias borradas).
        StrategyValidationError: SI el schema es inválido o los parámetros no lo cumplen.
    """
    path = _ready_db(db_path)
    with db.get_connection(path, write=True) as conn:
        if db.name_exists(conn, payload.name):
            raise StrategyConflictError(payload.name)
        if payload.parameter_schema is not None:
            check_parameter_schema(payload.parameter_schema)
        _validate_or_raise(payload.parameters, payload.parameter_schema)

        strategy_id = str(uuid6.uuid7())
        created_at_ms = now_ms()
        digest = compute_hash(payload.parameters)
        db.create_strategy_row(
            conn,
            strategy_id=strategy_id,
            name=payload.name,
            description=payload.description,
            parameter_schema=payload.parameter_schema,
            tags=payload.tags,
            created_at_ms=created_at_ms,
        )
        version = db.create_version_row(
            conn,
            strategy_id=strategy_id,
            version=1,
            parameters=payload.parameters,
            hash_=digest,
            created_at_ms=created_at_ms,
            created_by=user,
        )
    log_audit("create", strategy_id, 1, None, digest, {}, user)
    return version


# ------------------------------------------------------------------------------- get
def get_strategy(
    strategy_id: str, version: int | None = None, db_path: Path | None = None
) -> Strategy:
    """RF-2: estrategia con todas sus versiones; ``selected_version`` es la pedida o la última.

    Las estrategias con soft-delete se devuelven (con ``deleted_at_ms``) para auditoría.

    Raises:
        StrategyNotFoundError: SI ``strategy_id`` no existe.
        VersionNotFoundError: SI ``version`` se indica y no existe.
    """
    path = _ready_db(db_path)
    with db.get_connection(path) as conn:
        strategy = db.get_strategy_row(conn, strategy_id)
        if strategy is None:
            raise StrategyNotFoundError(strategy_id)
        strategy.versions = db.list_versions(conn, strategy_id, limit=-1)
    numbers = {v.version for v in strategy.versions}
    if version is not None and version not in numbers:
        raise VersionNotFoundError(strategy_id, version)
    strategy.selected_version = version if version is not None else max(numbers)
    return strategy


# ---------------------------------------------------------------------------- update
def update_strategy(
    strategy_id: str,
    payload: StrategyUpdate,
    user: str | None = None,
    db_path: Path | None = None,
) -> StrategyVersion:
    """RF-3: inserta una nueva versión sin mutar las anteriores (ADR-S01).

    Raises:
        StrategyNotFoundError: SI no existe o está borrada (soft-delete).
        StrategyValidationError: SI el patch es inválido o el resultado incumple el schema.
        NoChangesError: SI los parámetros resultantes tienen el hash de la versión actual.
    """
    path = _ready_db(db_path)
    with db.get_connection(path, write=True) as conn:
        strategy = db.get_strategy_row(conn, strategy_id)
        latest = db.get_latest_version(conn, strategy_id)
        if strategy is None or strategy.deleted_at_ms is not None or latest is None:
            raise StrategyNotFoundError(strategy_id)

        if payload.parameters is not None:
            new_params = payload.parameters
        else:
            new_params = apply_json_patch(latest.parameters, payload.parameters_patch or [])
        _validate_or_raise(new_params, strategy.parameter_schema)

        new_hash = compute_hash(new_params)
        if new_hash == latest.hash:
            raise NoChangesError
        version = db.create_version_row(
            conn,
            strategy_id=strategy_id,
            version=latest.version + 1,
            parameters=new_params,
            hash_=new_hash,
            created_at_ms=now_ms(),
            created_by=user,
        )
        db.update_strategy_metadata(
            conn, strategy_id, description=payload.description, tags=payload.tags
        )
    log_audit(
        "update",
        strategy_id,
        version.version,
        latest.hash,
        new_hash,
        _diff_summary(latest.parameters, new_params),
        user,
    )
    return version


# ---------------------------------------------------------------------------- delete
def delete_strategy(
    strategy_id: str,
    purge: bool = False,
    user: str | None = None,
    db_path: Path | None = None,
) -> None:
    """RF-4: soft-delete por defecto, hard-delete con ``purge``. Idempotente."""
    path = _ready_db(db_path)
    with db.get_connection(path, write=True) as conn:
        latest = db.get_latest_version(conn, strategy_id)
        if purge:
            changed = db.hard_delete_strategy(conn, strategy_id)
        else:
            changed = db.soft_delete_strategy(conn, strategy_id, now_ms())
    if changed and latest is not None:
        log_audit(
            "delete",
            strategy_id,
            latest.version,
            latest.hash,
            latest.hash,
            {"purge": purge},
            user,
        )


# ------------------------------------------------------------------------------ list
def list_strategies(
    filters: StrategyListFilters, db_path: Path | None = None
) -> StrategyListResponse:
    """RF-5: listado paginado con filtros; ``total_count`` ignora la paginación."""
    path = _ready_db(db_path)
    with db.get_connection(path) as conn:
        items, total = db.list_strategy_rows(conn, filters)
    return StrategyListResponse(
        items=items, total_count=total, limit=filters.limit, offset=filters.offset
    )


def list_versions(
    strategy_id: str, limit: int = 50, offset: int = 0, db_path: Path | None = None
) -> list[StrategyVersion]:
    """RF-10: versiones de una estrategia, paginadas y en orden ascendente.

    Raises:
        StrategyNotFoundError: SI ``strategy_id`` no existe.
    """
    path = _ready_db(db_path)
    with db.get_connection(path) as conn:
        if db.get_strategy_row(conn, strategy_id) is None:
            raise StrategyNotFoundError(strategy_id)
        return db.list_versions(conn, strategy_id, limit, offset)


# ------------------------------------------------------------------------------ diff
def diff_versions(
    strategy_id: str, from_v: int, to_v: int, db_path: Path | None = None
) -> DiffResult:
    """RF-6: diff semántico entre dos versiones (cualquier orden).

    Raises:
        StrategyNotFoundError / VersionNotFoundError: SI la estrategia o una versión no existe.
    """
    path = _ready_db(db_path)
    with db.get_connection(path) as conn:
        if db.get_strategy_row(conn, strategy_id) is None:
            raise StrategyNotFoundError(strategy_id)
        old = db.get_version(conn, strategy_id, from_v)
        new = db.get_version(conn, strategy_id, to_v)
    if old is None:
        raise VersionNotFoundError(strategy_id, from_v)
    if new is None:
        raise VersionNotFoundError(strategy_id, to_v)
    return compute_diff(old.parameters, new.parameters, from_v, to_v)


# ---------------------------------------------------------------------------- export
def export_strategy(
    strategy_id: str, fmt: ExportFormat = "json", db_path: Path | None = None
) -> bytes:
    """RF-10/ADR-S08: estrategia + todas las versiones + metadatos en JSON o YAML (UTF-8).

    Raises:
        StrategyNotFoundError: SI no existe.
        StrategyValidationError: SI ``fmt`` no es ``json`` ni ``yaml``.
    """
    if fmt not in ("json", "yaml"):
        msg = f"Unsupported export format: {fmt!r} (json|yaml)"
        raise StrategyValidationError(msg)
    strategy = get_strategy(strategy_id, db_path=db_path)
    document = {
        "exported_at_ms": now_ms(),
        "strategy": strategy.model_dump(mode="json", exclude={"selected_version"}),
    }
    if fmt == "json":
        return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")
