# Plan — `ft_strategy_manager` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquemas Core (`packages/finances-core/schemas/strategy.py`)

```python
from enum import Enum
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Optional, Literal
from datetime import datetime
import json
import hashlib

class StrategyCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    description: Optional[str] = Field(default=None, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_schema: Optional[dict[str, Any]] = None  # JSON Schema draft-07
    tags: list[str] = Field(default_factory=list)

class StrategyUpdate(BaseModel):
    parameters: Optional[dict[str, Any]] = None
    parameters_patch: Optional[list[dict[str, Any]]] = None  # JSON Patch RFC 6902
    description: Optional[str] = Field(default=None, max_length=500)
    tags: Optional[list[str]] = None

    @model_validator(mode="after")
    def check_one_of(self):
        if self.parameters is not None and self.parameters_patch is not None:
            raise ValueError("Provide either 'parameters' (full replace) or 'parameters_patch' (partial), not both")
        if self.parameters is None and self.parameters_patch is None:
            raise ValueError("Must provide either 'parameters' or 'parameters_patch'")
        return self

class StrategyVersion(BaseModel):
    version: int = Field(ge=1)
    parameters: dict[str, Any]
    hash: str = Field(pattern=r"^[a-f0-9]{64}$")  # SHA-256 hex
    created_at_ms: int
    created_by: Optional[str] = None

class Strategy(BaseModel):
    id: str  # UUID v7
    name: str
    description: Optional[str]
    parameter_schema: Optional[dict[str, Any]]
    tags: list[str]
    created_at_ms: int
    deleted_at_ms: Optional[int] = None
    versions: list[StrategyVersion] = Field(default_factory=list)

    @property
    def latest_version(self) -> Optional[StrategyVersion]:
        return max(self.versions, key=lambda v: v.version) if self.versions else None

class StrategySummary(BaseModel):
    id: str
    name: str
    latest_version: int
    updated_at_ms: int
    tags: list[str]
    hash_latest: str

class StrategyListResponse(BaseModel):
    items: list[StrategySummary]
    total_count: int
    limit: int
    offset: int

class DiffResult(BaseModel):
    from_version: int
    to_version: int
    added: dict[str, Any]
    removed: dict[str, Any]
    changed: dict[str, tuple[Any, Any]]  # key -> (old, new)
```

### 1.2 Utilidades de Hash y Validación

```python
# packages/finances-core/src/ft_strategy_manager/utils.py
import json
import hashlib
from jsonschema import validate, ValidationError
from typing import Any

def canonical_json(obj: dict[str, Any]) -> str:
    """JSON determinístico para hashing: keys ordenados, sin whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def compute_hash(parameters: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(parameters).encode()).hexdigest()

def validate_parameters(parameters: dict[str, Any], schema: Optional[dict[str, Any]]) -> tuple[bool, Optional[str]]:
    if schema is None:
        return True, None
    try:
        validate(instance=parameters, schema=schema)
        return True, None
    except ValidationError as e:
        return False, e.message
```

### 1.3 Esquema SQLite (`packages/finances-core/src/ft_strategy_manager/db.py`)

```sql
-- strategies table
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,                    -- UUID v7
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    parameter_schema TEXT,                  -- JSON
    tags TEXT NOT NULL DEFAULT '[]',        -- JSON array
    created_at_ms INTEGER NOT NULL,
    deleted_at_ms INTEGER                   -- NULL = active
);

-- strategy_versions table
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,          -- JSON
    hash TEXT NOT NULL,                     -- SHA-256 hex
    created_at_ms INTEGER NOT NULL,
    created_by TEXT,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
    UNIQUE(strategy_id, version)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_strategies_name ON strategies(name);
CREATE INDEX IF NOT EXISTS idx_versions_strategy_id ON strategy_versions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_versions_strategy_version ON strategy_versions(strategy_id, version);
```

---

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-core/src/ft_strategy_manager/cli.py`

```python
import typer
from typing import Optional
from rich.table import Table

app = typer.Typer(
    name="ft_strategy_manager",
    help="Gestión de estrategias: CRUD, versionado, diff, auditoría",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

@app.command()
def create(
    name: str = typer.Argument(..., help="Nombre único (snake_case, 3-64 chars)"),
    description: Optional[str] = typer.Option(None, "--desc", "-d", help="Descripción"),
    parameters_file: str = typer.Option(..., "--params", "-p", help="Archivo JSON con parámetros"),
    schema_file: Optional[str] = typer.Option(None, "--schema", "-s", help="Archivo JSON Schema (opcional)"),
    tags: list[str] = typer.Option([], "--tag", "-t", help="Tags (repetible)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Crea nueva estrategia v1."""

@app.command()
def get(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    version: Optional[int] = typer.Option(None, "--version", "-v", help="Versión específica (default: latest)"),
    format: Literal["table", "json", "yaml"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Obtiene estrategia completa con parámetros."""

@app.command()
def update(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    parameters_file: Optional[str] = typer.Option(None, "--params", "-p", help="JSON completo (reemplazo)"),
    patch_file: Optional[str] = typer.Option(None, "--patch", help="JSON Patch RFC 6902 (parcial)"),
    description: Optional[str] = typer.Option(None, "--desc", "-d"),
    tags: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Reemplaza tags (vacío = limpiar)"),
) -> None:
    """Crea nueva versión de estrategia existente."""

@app.command()
def delete(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    purge: bool = typer.Option(False, "--purge", help="Hard delete irreversible (requiere confirmación)"),
    force: bool = typer.Option(False, "--force", "-f", help="Saltar confirmación en --purge"),
) -> None:
    """Elimina estrategia (soft-delete por defecto)."""

@app.command()
def list(
    tag: Optional[str] = typer.Option(None, "--tag", help="Filtrar por tag exacto"),
    name_contains: Optional[str] = typer.Option(None, "--name", help="Substring en nombre"),
    include_deleted: bool = typer.Option(False, "--include-deleted"),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=500),
    offset: int = typer.Option(0, "--offset", "-o", min=0),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Lista estrategias con filtros y paginación."""

@app.command()
def diff(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    from_version: int = typer.Option(..., "--from", help="Versión origen"),
    to_version: int = typer.Option(..., "--to", help="Versión destino"),
    format: Literal["table", "json", "yaml"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Diff semántico entre dos versiones."""

@app.command()
def versions(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=500),
    offset: int = typer.Option(0, "--offset", "-o", min=0),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Lista todas las versiones de una estrategia."""

@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: Literal["rich", "json"] = typer.Option("rich", "--log-format"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
```

### 2.2 API (FastAPI) — `packages/finances-core/src/ft_strategy_manager/api.py`

```python
from fastapi import APIRouter, HTTPException, Query, Path, status, Response
from fastapi.responses import FileResponse
from typing import Optional, Literal
import yaml

router = APIRouter(prefix="/strategies", tags=["strategies"])

@router.post("", status_code=status.HTTP_201_CREATED, response_model=StrategyVersion)
async def create_strategy(payload: StrategyCreate, response: Response):
    """Crear estrategia v1. Retorna versión creada + Location header."""

@router.get("", response_model=StrategyListResponse)
async def list_strategies(
    tag: Optional[str] = Query(None),
    name_contains: Optional[str] = Query(None, alias="name"),
    include_deleted: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Listar estrategias con filtros."""

@router.get("/{strategy_id}", response_model=Strategy)
async def get_strategy(
    strategy_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    version: Optional[int] = Query(None, ge=1),
):
    """Obtener estrategia (versión específica o latest)."""

@router.put("/{strategy_id}", response_model=StrategyVersion)
async def update_strategy(
    strategy_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    payload: StrategyUpdate = ...,
):
    """Crear nueva versión."""

@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(
    strategy_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    purge: bool = Query(False),
):
    """Soft/hard delete."""

@router.get("/{strategy_id}/versions", response_model=list[StrategyVersion])
async def list_versions(
    strategy_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Listar versiones paginadas."""

@router.get("/{strategy_id}/diff", response_model=DiffResult)
async def diff_versions(
    strategy_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    from_version: int = Query(..., ge=1, alias="from"),
    to_version: int = Query(..., ge=1, alias="to"),
):
    """Diff semántico entre versiones."""

@router.get("/{strategy_id}/export")
async def export_strategy(
    strategy_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    format: Literal["json", "yaml"] = Query("json"),
):
    """Exportar estrategia completa (todas las versiones)."""
```

---

## 3. Algoritmos Clave

### 3.1 `create` — Crear Estrategia v1

```
FUNCTION create_strategy(payload: StrategyCreate) -> StrategyVersion:
    1. validate_name_unique(payload.name) → raise 409 if exists
    2. valid, err = validate_parameters(payload.parameters, payload.parameter_schema)
       IF NOT valid: raise 422(err)
    3. hash = compute_hash(payload.parameters)
    4. now_ms = current_timestamp_ms()
    5. strategy_id = uuid7()
    6. INSERT INTO strategies (id, name, description, parameter_schema, tags, created_at_ms)
       VALUES (strategy_id, name, desc, schema_json, tags_json, now_ms)
    7. INSERT INTO strategy_versions (strategy_id, version, parameters_json, hash, created_at_ms, created_by)
       VALUES (strategy_id, 1, parameters_json, hash, now_ms, current_user_or_none())
    8. log_audit("create", strategy_id, version=1, hash_after=hash, diff_summary={})
    9. RETURN StrategyVersion(version=1, parameters=payload.parameters, hash=hash, created_at_ms=now_ms)
```

### 3.2 `update` — Nueva Versión (Inmutable)

```
FUNCTION update_strategy(strategy_id: str, payload: StrategyUpdate) -> StrategyVersion:
    1. strategy = SELECT * FROM strategies WHERE id = strategy_id AND deleted_at_ms IS NULL
       IF NOT found: raise 404
    2. latest = SELECT * FROM strategy_versions WHERE strategy_id = ? ORDER BY version DESC LIMIT 1
    3. new_params = IF payload.parameters:
                          payload.parameters
                      ELSE:
                          apply_json_patch(latest.parameters, payload.parameters_patch)
    4. valid, err = validate_parameters(new_params, strategy.parameter_schema)
       IF NOT valid: raise 422(err)
    5. new_hash = compute_hash(new_params)
       IF new_hash == latest.hash: raise 400("No changes detected")
    6. new_version = latest.version + 1
    7. now_ms = current_timestamp_ms()
    8. INSERT INTO strategy_versions (strategy_id, version, parameters_json, hash, created_at_ms, created_by)
       VALUES (strategy_id, new_version, new_params_json, new_hash, now_ms, current_user_or_none())
    9. IF payload.description: UPDATE strategies SET description = ? WHERE id = ?
    10. IF payload.tags is not null: UPDATE strategies SET tags = ? WHERE id = ?
    11. diff_summary = compute_diff(latest.parameters, new_params)
    12. log_audit("update", strategy_id, version=new_version, hash_before=latest.hash, hash_after=new_hash, diff_summary=diff_summary)
    13. RETURN StrategyVersion(version=new_version, parameters=new_params, hash=new_hash, created_at_ms=now_ms)
```

### 3.3 `diff` — Diff Semántico

```
FUNCTION diff_versions(strategy_id: str, from_v: int, to_v: int) -> DiffResult:
    1. v_from = SELECT parameters_json FROM strategy_versions WHERE strategy_id=? AND version=?
    2. v_to = SELECT parameters_json FROM strategy_versions WHERE strategy_id=? AND version=?
    3. IF either NULL: raise 404
    4. from_params = json_loads(v_from), to_params = json_loads(v_to)
    5. all_keys = set(from_params) | set(to_params)
    6. added = {k: to_params[k] for k in all_keys if k not in from_params}
    7. removed = {k: from_params[k] for k in all_keys if k not in to_params}
    8. changed = {k: (from_params[k], to_params[k]) for k in all_keys
                  if k in from_params and k in to_params and from_params[k] != to_params[k]}
    9. RETURN DiffResult(from_version=from_v, to_version=to_v, added, removed, changed)
```

### 3.4 `list` — Listado Paginado con Filtros

```
FUNCTION list_strategies(filters) -> StrategyListResponse:
    1. base_query = "SELECT id, name, tags, created_at_ms, deleted_at_ms FROM strategies WHERE 1=1"
    2. params = []
    3. IF NOT filters.include_deleted: base_query += " AND deleted_at_ms IS NULL"
    4. IF filters.tag: base_query += " AND JSON_CONTAINS(tags, ?)"; params.append(json_dumps(filters.tag))
    5. IF filters.name_contains: base_query += " AND name LIKE ?"; params.append(f"%{filters.name_contains}%")
    6. count_query = "SELECT COUNT(*) FROM (" + base_query + ")"
    7. total = execute_scalar(count_query, params)
    8. data_query = base_query + " ORDER BY created_at_ms DESC LIMIT ? OFFSET ?"
    9. params.extend([filters.limit, filters.offset])
    10. rows = execute_query(data_query, params)
    11. FOR each row: fetch latest version hash from strategy_versions
    12. RETURN StrategyListResponse(items=[...], total_count=total, limit, offset)
```

---

## 4. Estructura de Archivos Objetivo

```
packages/finances-core/
├── src/
│   └── finances_core/
│       ├── schemas/
│       │   ├── __init__.py
│       │   ├── setup.py
│       │   ├── market_data.py
│       │   └── strategy.py          # ← NUEVO: modelos Pydantic
│       └── ft_strategy_manager/
│           ├── __init__.py
│           ├── cli.py               # Typer app
│           ├── api.py               # FastAPI router
│           ├── service.py           # Lógica compartida (CRUD, diff, validación)
│           ├── db.py                # SQLite init, queries, connection pool
│           ├── utils.py             # hash, json_canonical, jsonschema validation
│           └── logging.py           # configure_logging (reutiliza patrón ft_setup)
└── share/
    ├── tasks.db                     # SQLite WAL (ft_setup)
    └── strategies.db                # SQLite WAL (ESTA tool)
```

---

## 5. Dependencias Adicionales (pyproject.toml - finances-core)

```toml
# Añadir a dependencies existentes:
dependencies = [
    # ... existentes ...
    "jsonschema>=4.21",        # Validación JSON Schema (RF-7)
    "jsonpatch>=1.33",         # RFC 6902 JSON Patch (RF-3)
    "pyyaml>=6.0",             # Export YAML
]
```

---

## 6. Decisiones Arquitectónicas Clave (ADR)

| ADR | Decisión | Justificación |
|-----|----------|---------------|
| ADR-S01 | Inmutabilidad de versiones: nunca UPDATE/DELETE en `strategy_versions`, solo INSERT | Auditoría, reproducibilidad, compliance (RF-4, RF-8) |
| ADR-S02 | Hash SHA-256 de JSON canónico (keys ordenados) como identificación de contenido | Detectar cambios reales, idempotencia, cache keys |
| ADR-S03 | JSON Schema (draft-07) opcional en `create` para validar parámetros | Flexibilidad: equipos maduros usan schema; equipos rápidos modo permisivo |
| ADR-S04 | JSON Patch RFC 6902 para updates parciales | Estándar web, granularidad fina, evita enviar objeto completo |
| ADR-S05 | Soft-delete por defecto (`deleted_at_ms`), hard-delete solo con `purge=true` | Protección contra borrado accidental; compliance requiere retención |
| ADR-S06 | `strategies.db` separado de `tasks.db` | Aislamiento de dominios; distinto ciclo de vida, backup, retención |
| ADR-S07 | `created_by` opcional (header `X-User-ID`) para auditoría | Preparado para auth futuro sin breaking changes |
| ADR-S08 | Export YAML/JSON incluye metadatos completos + todas las versiones | Portabilidad, GitOps futuro, revisión humana |

---

## 7. Integración con `finances-core` Existente

- Reutiliza `FinancesSettings` para `STRATEGIES_DB_PATH` (default `./share/strategies.db`)
- Reutiliza `configure_logging` de `ft_setup.logging` (DRY)
- Reutiliza `SQLTaskManager` patrón para conexiones WAL thread-safe
- Registra router en `ft_api_service` main app via `include_router(strategy_router)`