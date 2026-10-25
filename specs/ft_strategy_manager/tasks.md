# Tasks — `ft_strategy_manager` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).

## Fase 0: Modelos y Esquemas Compartidos

- [ ] **T0.1**: Añadir `StrategyCreate`, `StrategyUpdate`, `Strategy`, `StrategyVersion`, `StrategySummary`, `StrategyListResponse`, `DiffResult` en `packages/finances-core/src/finances_core/schemas/strategy.py`
  - **Hecho cuando**: `from finances_core.schemas.strategy import StrategyCreate` importa; modelos validan ejemplos JSON; MyPy strict pasa.
- [ ] **T0.2**: Añadir `jsonschema`, `jsonpatch`, `pyyaml` a `packages/finances-core/pyproject.toml` dependencies
  - **Hecho cuando**: `uv sync --package finances-core` resuelve; `uv run python -c "import jsonschema, jsonpatch, yaml"` OK.
- [ ] **T0.3**: Implementar `canonical_json`, `compute_hash`, `validate_parameters` en `packages/finances-core/src/ft_strategy_manager/utils.py`
  - **Hecho cuando**: Tests unitarios: hash determinístico (mismo input = mismo hash); keys ordenados; JSON Schema valida/rechaza correctamente; JSON Patch aplica correctamente.

## Fase 1: Capa de Persistencia (SQLite WAL)

- [ ] **T1.1**: Implementar `packages/finances-core/src/ft_strategy_manager/db.py` con:
  - `init_db(db_path: Path)` → crea tablas + índices + habilita WAL
  - `get_connection()` → thread-local connection pool (Art. 16 Constitución)
  - Helpers: `execute`, `execute_many`, `fetch_one`, `fetch_all`
  - **Hecho cuando**: `init_db(tmp_path)` crea `strategies.db` con esquema correcto; `PRAGMA journal_mode=WAL` retorna `wal`; conexiones concurrentes no bloquean lecturas.
- [ ] **T1.2**: Implementar CRUD SQL en `db.py`:
  - `create_strategy_row(...)`, `get_strategy_row(id)`, `soft_delete_strategy(id)`, `hard_delete_strategy(id)`
  - `create_version_row(...)`, `get_version(strategy_id, version)`, `list_versions(strategy_id, limit, offset)`, `get_latest_version(strategy_id)`
  - **Hecho cuando**: Tests de integración con DB real verifican: insert, select, unique constraints, foreign key cascade, soft/hard delete.

## Fase 2: Servicio Compartido (Lógica de Negocio)

- [ ] **T2.1**: Implementar `create_strategy(payload: StrategyCreate) -> StrategyVersion` en `service.py`
  - **Hecho cuando**: Test: crea v1, hash correcto, persiste en DB, log_audit llamado con campos esperados.
- [ ] **T2.2**: Implementar `get_strategy(strategy_id: str, version: Optional[int]) -> Strategy`
  - **Hecho cuando**: Test: retorna latest por defecto; versión específica si se pasa; 404 si no existe; incluye todas las versiones en `Strategy.versions`.
- [ ] **T2.3**: Implementar `update_strategy(strategy_id: str, payload: StrategyUpdate) -> StrategyVersion`
  - **Hecho cuando**: Test: crea v2, v3...; inmutabilidad (v1 intacto); hash cambia solo si parámetros cambian; JSON Patch parcial funciona; validación schema; rechaza sin cambios.
- [ ] **T2.4**: Implementar `delete_strategy(strategy_id: str, purge: bool) -> None`
  - **Hecho cuando**: Test: `purge=False` → `deleted_at_ms` seteado, oculta de listados, versiones persisten; `purge=True` → filas eliminadas en cascada; idempotente (no error si ya borrado).
- [ ] **T2.5**: Implementar `list_strategies(filters) -> StrategyListResponse`
  - **Hecho cuando**: Test: paginación correcta; filtros tag/name/include_deleted; total_count exacto; latest_version y hash_latest poblados.
- [ ] **T2.6**: Implementar `diff_versions(strategy_id: str, from_v: int, to_v: int) -> DiffResult`
  - **Hecho cuando**: Test: added/removed/changed correctos; tipos JSON preservados; orden de versiones irrelevante (from < to o from > to); 404 si versión inexistente.
- [ ] **T2.7**: Implementar `export_strategy(strategy_id: str, format: Literal["json","yaml"]) -> bytes`
  - **Hecho cuando**: Test: output válido JSON/YAML; incluye estrategia + todas las versiones + metadatos; round-trip parseable.

## Fase 3: Logging Dual y Auditoría

- [ ] **T3.1**: Reutilizar/extender `configure_logging` de `ft_setup` en `ft_strategy_manager/logging.py`
  - **Hecho cuando**: CLI usa Rich; API usa JSON; loggers de librería agnósticos.
- [ ] **T3.2**: Implementar `log_audit(action, strategy_id, version, hash_before, hash_after, diff_summary, user)` en `service.py`
  - **Hecho cuando**: Logs estructurados contienen todos los campos; secrets en parámetros enmascarados (Art. 9); nivel INFO para mutaciones.

## Fase 4: CLI (Typer)

- [ ] **T4.1**: Implementar `cli.py` con todos los comandos: `create`, `get`, `update`, `delete`, `list`, `diff`, `versions`
  - **Hecho cuando**: `uv run finances-core strategy --help` muestra 7 subcomandos; cada uno tiene help descriptivo.
- [ ] **T4.2**: Implementar flags globales: `--verbose/-v`, `--quiet/-q`, `--log-format [rich|json]`
  - **Hecho cuando**: Afectan nivel y formato de logging correctamente.
- [ ] **T4.3**: Implementar formatos de salida: `--format [table|json|yaml]` por comando
  - **Hecho cuando**: `list`/`versions` → table/json; `get`/`diff`/`export` → table/json/yaml; Rich tables bonitas con colores.
- [ ] **T4.4**: Manejo de archivos de entrada: `--params`, `--patch`, `--schema` (path a archivo JSON)
  - **Hecho cuando**: Lee archivo, parsea JSON, valida existencia; error claro si archivo inválido/no existe.
- [ ] **T4.5**: Exit codes: `0` OK, `1` not found, `2` validation error, `3` conflict (duplicate name), `4` IO error
  - **Hecho cuando**: Tests `pytest -k cli_exit_codes` pasan.

## Fase 5: API (FastAPI)

- [ ] **T5.1**: Implementar `api.py` con router `/strategies` y 8 endpoints (ver plan.md §2.2)
  - **Hecho cuando**: `ft_serve` (paquete `finances-api`) levanta; OpenAPI docs en `/docs` muestran todos los endpoints.
- [ ] **T5.2**: Validación de request/response con modelos Pydantic (StrategyCreate, StrategyUpdate, etc.)
  - **Hecho cuando**: Request inválido → 422 con detail estructurado; Response valida vs modelo de salida.
- [ ] **T5.3**: `POST /strategies` retorna `201 Created` + `Location: /strategies/{id}` header
  - **Hecho cuando**: Test verifica status, header, body = StrategyVersion.
- [ ] **T5.4**: `GET /strategies/{id}/export` retorna `FileResponse` con `Content-Disposition: attachment`
  - **Hecho cuando**: Test verifica header, content-type (application/json o yaml), body parseable.

## Fase 6: Tests y Calidad (Art. 7, 17 Constitución)

- [ ] **T6.1**: Tests unitarios: `utils.py` (hash, canonical_json, validate_parameters, jsonpatch)
  - **Hecho cuando**: `pytest packages/finances-core/tests/unit/test_strategy_utils.py -v` pasa; cobertura ≥ 95%.
- [ ] **T6.2**: Tests unitarios: `service.py` (mock DB) — create, get, update, delete, list, diff, export
  - **Hecho cuando**: `pytest packages/finances-core/tests/unit/test_strategy_service.py -v` pasa; cobertura ≥ 90%.
- [ ] **T6.3**: Tests de integración DB: `db.py` con SQLite real (temp file)
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_db.py -v` pasa; WAL mode verificado.
- [ ] **T6.4**: Tests de integración CLI: todos los comandos con temp DB y env vars
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_cli.py -v` pasa.
- [ ] **T6.5**: Tests de integración API: todos los endpoints con `httpx.AsyncClient` (testserver)
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_api.py -v` pasa.
- [ ] **T6.6**: Tests de contrato: CLI vs API producen mismo resultado para mismos inputs
  - **Hecho cuando**: `pytest packages/finances-core/tests/contract/test_strategy_cli_api_parity.py -v` pasa.
- [ ] **T6.7**: Configurar `ruff` + `mypy --strict` en `finances-core` (ya en T0.2/T6.4 de ft_setup)
  - **Hecho cuando**: `uv run ruff check packages/finances-core` && `uv run mypy --strict packages/finances-core` → 0 errors.

## Fase 7: Documentación y Cierre

- [ ] **T7.1**: Actualizar `README.md` sección "Inicio Rápido" con ejemplos `ft_strategy_manager create/get/list/update/diff`
  - **Hecho cuando**: README refleja comandos reales con ejemplos copy-pasteables.
- [ ] **T7.2**: Verificar matriz Art. 20 Constitución (100% dimensiones) via checklist interno
  - **Hecho cuando**: Checklist firmado en PR description; CI pipeline verde.
- [ ] **T7.3**: Registrar router en `ft_api_service` main app (integración cross-package)
  - **Hecho cuando**: `ft_api_service` importa e incluye `strategy_router`; health-check incluye strategies DB.