# Tasks — `ft_strategy_manager` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).

## Fase 0: Modelos y Esquemas Compartidos

- [x] **T0.1**: Añadir `StrategyCreate`, `StrategyUpdate`, `Strategy`, `StrategyVersion`, `StrategySummary`, `StrategyListResponse`, `DiffResult` en `packages/finances-core/src/finances_core/schemas/strategy.py`
  - **Hecho cuando**: `from finances_core.schemas.strategy import StrategyCreate` importa; modelos validan ejemplos JSON; MyPy strict pasa.
  - **Hecho**: `finances_core.schemas.strategy` con los 7 modelos (+ `StrategyListFilters`; `StrategyVersion` incluye `strategy_id` porque RF-1/RF-3 lo devuelven); `tests/unit/test_strategy_schemas.py` (22 passed); `mypy --strict src` â†’ Success.

- [x] **T0.2**: Añadir `jsonschema`, `jsonpatch`, `pyyaml` a `packages/finances-core/pyproject.toml` dependencies
  - **Hecho cuando**: `pip install -e packages/finances-core` resuelve; `python -c "import jsonschema, jsonpatch, yaml"` OK.
  - **Hecho**: `jsonschema>=4.21`, `jsonpatch>=1.33`, `pyyaml>=6.0` en `dependencies` (+ stubs `types-*` en `dev`); `pip install -e packages/finances-core` OK y `python -c "import jsonschema, jsonpatch, yaml"` â†’ OK.

- [x] **T0.3**: Implementar `canonical_json`, `compute_hash`, `validate_parameters` en `packages/finances-core/src/ft_strategy_manager/utils.py`
  - **Hecho cuando**: Tests unitarios: hash determinístico (mismo input = mismo hash); keys ordenados; JSON Schema valida/rechaza correctamente; JSON Patch aplica correctamente.
  - **Hecho**: `ft_strategy_manager/utils.py` (+ `errors.py`): `canonical_json`, `compute_hash`, `validate_parameters`, `check_parameter_schema`, `apply_json_patch`, `compute_diff`, `mask_secrets`. RED: colecciÃ³n falla sin `utils.py`; GREEN: `test_strategy_utils.py` 19 passed.

## Fase 1: Capa de Persistencia (SQLite WAL)

- [x] **T1.1**: Implementar `packages/finances-core/src/ft_strategy_manager/db.py` con:
  - `init_db(db_path: Path)` → crea tablas + índices + habilita WAL
  - `get_connection()` → thread-local connection pool (Art. 16 Constitución)
  - Helpers: `execute`, `execute_many`, `fetch_one`, `fetch_all`
  - **Hecho cuando**: `init_db(tmp_path)` crea `strategies.db` con esquema correcto; `PRAGMA journal_mode=WAL` retorna `wal`; conexiones concurrentes no bloquean lecturas.
  - **Hecho**: `ft_strategy_manager/db.py`: `init_db`, `get_connection` (conexiÃ³n propia por llamada, patrÃ³n `SQLTaskManager`/Art. 16; `write=True` â†’ `BEGIN IMMEDIATE`), `execute`, `execute_many`, `fetch_one`, `fetch_all`. `tests/integration/test_strategy_db.py`: esquema+Ã­ndices, `PRAGMA journal_mode` â†’ `wal`, lector concurrente no bloqueado por escritor abierto, rollback. RED: colecciÃ³n falla sin `db.py`; GREEN: 16 passed.

- [x] **T1.2**: Implementar CRUD SQL en `db.py`:
  - `create_strategy_row(...)`, `get_strategy_row(id)`, `soft_delete_strategy(id)`, `hard_delete_strategy(id)`
  - `create_version_row(...)`, `get_version(strategy_id, version)`, `list_versions(strategy_id, limit, offset)`, `get_latest_version(strategy_id)`
  - **Hecho cuando**: Tests de integración con DB real verifican: insert, select, unique constraints, foreign key cascade, soft/hard delete.
  - **Hecho**: CRUD SQL en `db.py` (`create_strategy_row`, `get_strategy_row`, `soft_delete_strategy`, `hard_delete_strategy`, `create_version_row`, `get_version`, `list_versions`, `get_latest_version`, `update_strategy_metadata`, `list_strategy_rows`). Tests de integraciÃ³n con DB real: unique(name) â†’ `StrategyConflictError`, unique(strategy_id, version), FK, cascade en hard-delete, soft-delete idempotente (16 passed).

## Fase 2: Servicio Compartido (Lógica de Negocio)

- [x] **T2.1**: Implementar `create_strategy(payload: StrategyCreate) -> StrategyVersion` en `service.py`
  - **Hecho cuando**: Test: crea v1, hash correcto, persiste en DB, log_audit llamado con campos esperados.
  - **Hecho**: `service.create_strategy` (v1 atÃ³mica, UUID v7, hash SHA-256, 409/422, `log_audit`). RED: colecciÃ³n falla sin `service.py`; GREEN: `test_strategy_service.py` create* (6 tests) passed.

- [x] **T2.2**: Implementar `get_strategy(strategy_id: str, version: Optional[int]) -> Strategy`
  - **Hecho cuando**: Test: retorna latest por defecto; versión específica si se pasa; 404 si no existe; incluye todas las versiones en `Strategy.versions`.
  - **Hecho**: `service.get_strategy(strategy_id, version)` â†’ `Strategy` con todas las versiones y `selected_version` (pedida o Ãºltima); `StrategyNotFoundError` / `VersionNotFoundError` (404). Tests get* passed.

- [x] **T2.3**: Implementar `update_strategy(strategy_id: str, payload: StrategyUpdate) -> StrategyVersion`
  - **Hecho cuando**: Test: crea v2, v3...; inmutabilidad (v1 intacto); hash cambia solo si parámetros cambian; JSON Patch parcial funciona; validación schema; rechaza sin cambios.
  - **Hecho**: `service.update_strategy`: v2, v3...; v1 intacto; hash solo cambia con los parÃ¡metros; JSON Patch parcial; schema validado; `NoChangesError` sin cambios; 404 si falta o estÃ¡ borrada. Tests update* (8) passed.

- [x] **T2.4**: Implementar `delete_strategy(strategy_id: str, purge: bool) -> None`
  - **Hecho cuando**: Test: `purge=False` → `deleted_at_ms` seteado, oculta de listados, versiones persisten; `purge=True` → filas eliminadas en cascada; idempotente (no error si ya borrado).
  - **Hecho**: `service.delete_strategy(strategy_id, purge)`: soft (`deleted_at_ms`, oculta del listado, versiones persisten), purge en cascada, idempotente. Tests delete*/purge* passed.

- [x] **T2.5**: Implementar `list_strategies(filters) -> StrategyListResponse`
  - **Hecho cuando**: Test: paginación correcta; filtros tag/name/include_deleted; total_count exacto; latest_version y hash_latest poblados.
  - **Hecho**: `service.list_strategies(filters)` (+ `service.list_versions`): paginaciÃ³n, filtros tag/name/include_deleted, `total_count` exacto, `latest_version`/`hash_latest` poblados (un Ãºnico JOIN, sin N+1).

- [x] **T2.6**: Implementar `diff_versions(strategy_id: str, from_v: int, to_v: int) -> DiffResult`
  - **Hecho cuando**: Test: added/removed/changed correctos; tipos JSON preservados; orden de versiones irrelevante (from < to o from > to); 404 si versión inexistente.
  - **Hecho**: `service.diff_versions`: added/removed/changed con tipos JSON preservados (`1` vs `1.0` vs `true`), cualquier orden de versiones, 404 si falta estrategia/versiÃ³n. Tests diff* passed.

- [x] **T2.7**: Implementar `export_strategy(strategy_id: str, format: Literal["json","yaml"]) -> bytes`
  - **Hecho cuando**: Test: output válido JSON/YAML; incluye estrategia + todas las versiones + metadatos; round-trip parseable.
  - **Hecho**: `service.export_strategy(strategy_id, fmt)` â†’ bytes UTF-8; JSON y YAML con estrategia + todas las versiones + `exported_at_ms`; round-trip JSON==YAML verificado; formato desconocido â†’ `StrategyValidationError`.

## Fase 3: Logging Dual y Auditoría

- [x] **T3.1**: Reutilizar/extender `configure_logging` de `ft_setup` en `ft_strategy_manager/logging.py`
  - **Hecho cuando**: CLI usa Rich; API usa JSON; loggers de librería agnósticos.
  - **Hecho**: `ft_strategy_manager/logging.py`: `configure(mode, verbose, quiet, log_format)` sobre `finances_core.logging.configure_logging` con `component="ft_strategy_manager"`. `test_strategy_logging.py` (4 passed): CLI â†’ Rich en stderr (stdout limpio), API â†’ JSON en stdout con `component`, `--verbose`/`--quiet` fijan DEBUG/WARNING. (Tests escritos junto al mÃ³dulo: sin RED observado.)

- [x] **T3.2**: Implementar `log_audit(action, strategy_id, version, hash_before, hash_after, diff_summary, user)` en `service.py`
  - **Hecho cuando**: Logs estructurados contienen todos los campos; secrets en parámetros enmascarados (Art. 9); nivel INFO para mutaciones.
  - **Hecho**: `service.log_audit(action, strategy_id, version, hash_before, hash_after, diff_summary, user)` â†’ evento INFO `strategy_audit`; `diff_summary` pasa por `mask_secrets` (claves password/token/secret/api_key/credential/private_key). Tests con `structlog.testing.capture_logs`: campos completos en create/update/delete y valores secretos ausentes del log.

## Fase 4: CLI (Typer)

- [x] **T4.1**: Implementar `cli.py` con todos los comandos: `create`, `get`, `update`, `delete`, `list`, `diff`, `versions`
  - **Hecho cuando**: `ft_strategy_manager --help` muestra 7 subcomandos; cada uno tiene help descriptivo.
  - **Hecho**: `ft_strategy_manager/cli.py` con `create`, `get`, `update`, `delete`, `list`, `diff`, `versions` (+ `export`, simetrÃ­a con `GET /strategies/{id}/export`, Art. 1); entry point `ft_strategy_manager` en `pyproject.toml`. `ft_strategy_manager --help` (exe real) lista los subcomandos con help descriptivo; `test_help_lists_all_subcommands_with_descriptions` y `test_every_subcommand_has_help` (8 comandos). RED: colecciÃ³n falla sin `cli.py`; GREEN: `test_strategy_cli.py` 31 passed.

- [x] **T4.2**: Implementar flags globales: `--verbose/-v`, `--quiet/-q`, `--log-format [rich|json]`
  - **Hecho cuando**: Afectan nivel y formato de logging correctamente.
  - **Hecho**: Callback `--verbose/-v`, `--quiet/-q`, `--log-format [rich|json]` â†’ `ft_strategy_manager.logging.configure("cli", ...)`. `test_quiet_hides_info_audit_and_verbose_shows_it` y `test_log_format_json_emits_audit_on_stderr_only` (auditorÃ­a JSON solo en stderr, stdout intacto).

- [x] **T4.3**: Implementar formatos de salida: `--format [table|json|yaml]` por comando
  - **Hecho cuando**: `list`/`versions` → table/json; `get`/`diff`/`export` → table/json/yaml; Rich tables bonitas con colores.
  - **Hecho**: `--format table|json|yaml` en `get`/`diff`/`export`(json|yaml), `table|json` en `list`/`versions`/`create`/`update`; Rich tables con colores. Tests de salida json/yaml/table por comando en `test_strategy_cli.py`.

- [x] **T4.4**: Manejo de archivos de entrada: `--params`, `--patch`, `--schema` (path a archivo JSON)
  - **Hecho cuando**: Lee archivo, parsea JSON, valida existencia; error claro si archivo inválido/no existe.
  - **Hecho**: `--params`, `--patch`, `--schema` (+ `export --output`): `_read_json_file` â†’ archivo inexistente/ilegible = exit 4 ("Cannot read ..."); JSON roto, tipo equivocado (objeto/array), no UTF-8 o schema invÃ¡lido = exit 2 con mensaje claro. `test_input_file_errors_are_clear`.

- [x] **T4.5**: Exit codes: `0` OK, `1` not found, `2` validation error, `3` conflict (duplicate name), `4` IO error
  - **Hecho cuando**: Tests `pytest -k cli_exit_codes` pasan.
  - **Hecho**: Exit codes 0 OK / 1 no encontrado / 2 validaciÃ³n / 3 conflicto / 4 E/S; `pytest -k cli_exit_codes` â†’ 8 passed. `delete --purge` declinado (sin `--force`) sale con 1 (Abort de click, no es "no encontrado").

## Fase 5: API (FastAPI)

- [x] **T5.1**: Implementar `api.py` con router `/strategies` y 8 endpoints (ver plan.md §2.2)
  - **Hecho cuando**: `ft_serve` (paquete `finances-api`) levanta; OpenAPI docs en `/docs` muestran todos los endpoints.
  - **Hecho**: `ft_strategy_manager/api.py`: router `/strategies` con las 8 operaciones (POST/GET `/strategies`, GET/PUT/DELETE `/{id}`, GET `/{id}/versions`, `/{id}/diff`, `/{id}/export`), nombres de ruta `strategies_*`, delegando en `service` vÃ­a `run_in_threadpool` (Art. 3) y `X-User-ID` â†’ `created_by`. Montado en `finances_api.app.create_app()`; `GET /docs` â†’ 200 y `/openapi.json` lista las 5 rutas `/strategies*` (verificado vÃ­a ASGI sobre la app compuesta; no se levantÃ³ `ft_serve` como proceso: el lanzamiento en segundo plano fue denegado por el entorno).

- [x] **T5.2**: Validación de request/response con modelos Pydantic (StrategyCreate, StrategyUpdate, etc.)
  - **Hecho cuando**: Request inválido → 422 con detail estructurado; Response valida vs modelo de salida.
  - **Hecho**: Request invÃ¡lido â†’ 422 con `detail` estructurado de FastAPI (nombre, parÃ¡metros no JSON, `update` sin/ambos `parameters`/`parameters_patch`, `limit`>500, `version`<1); errores de negocio â†’ 404 `{"detail": "Strategy not found"}`, 409, 422 (schema/patch invÃ¡lido), 400 `No changes detected`; respuestas validadas por `response_model` (`StrategyVersion`, `Strategy`, `StrategyListResponse`, `DiffResult`). `test_create_conflict_and_validation_errors`, `test_update_replace_patch_and_errors`.

- [x] **T5.3**: `POST /strategies` retorna `201 Created` + `Location: /strategies/{id}` header
  - **Hecho cuando**: Test verifica status, header, body = StrategyVersion.
  - **Hecho**: `POST /strategies` â†’ `201` + `Location: /strategies/{id}` + cuerpo `StrategyVersion`; `test_create_returns_201_location_and_version_body`.

- [x] **T5.4**: `GET /strategies/{id}/export` retorna `FileResponse` con `Content-Disposition: attachment`
  - **Hecho cuando**: Test verifica header, content-type (application/json o yaml), body parseable.
  - **Hecho**: `GET /strategies/{id}/export` â†’ `Response` con `Content-Disposition: attachment; filename="strategy-{id}.{json|yaml}"` y content-type `application/json`/`application/yaml` (bytes en memoria, no `FileResponse`: no hay archivo en disco). `test_export_json_and_yaml_as_attachment`.

## Fase 6: Tests y Calidad (Art. 7, 17 Constitución)

- [x] **T6.1**: Tests unitarios: `utils.py` (hash, canonical_json, validate_parameters, jsonpatch)
  - **Hecho cuando**: `pytest packages/finances-core/tests/unit/test_strategy_utils.py -v` pasa; cobertura ≥ 95%.
  - **Hecho**: `pytest tests/unit/test_strategy_utils.py` â†’ 19 passed; cobertura de `ft_strategy_manager/utils.py` 100% (54/54, `--cov-report=term-missing`).

- [x] **T6.2**: Tests unitarios: `service.py` (mock DB) — create, get, update, delete, list, diff, export
  - **Hecho cuando**: `pytest packages/finances-core/tests/unit/test_strategy_service.py -v` pasa; cobertura ≥ 90%.
  - **Hecho**: `pytest tests/unit/test_strategy_service.py -v` â†’ 35 passed; cobertura de `service.py` 99%. Usa SQLite temporal real (aislada por `FT_PROJECT_ROOT`) en lugar de un mock: las reglas dependen de las restricciones de la base.

- [x] **T6.3**: Tests de integración DB: `db.py` con SQLite real (temp file)
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_db.py -v` pasa; WAL mode verificado.
  - **Hecho**: `pytest tests/integration/test_strategy_db.py -v` â†’ 16 passed; WAL verificado (`PRAGMA journal_mode` = `wal`); cobertura de `db.py` 97%.

- [x] **T6.4**: Tests de integración CLI: todos los comandos con temp DB y env vars
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_cli.py -v` pasa.
  - **Hecho**: `pytest packages/finances-core/tests/integration/test_strategy_cli.py -v` â†’ 31 passed (todos los comandos con base temporal y `FT_PROJECT_ROOT`/`FT_STRATEGIES_DB_PATH`).

- [x] **T6.5**: Tests de integración API: todos los endpoints con `httpx.AsyncClient` (testserver)
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_api.py -v` pasa.
  - **Hecho**: `pytest packages/finances-core/tests/integration/test_strategy_api.py -v` â†’ 11 passed (`httpx.AsyncClient` + `ASGITransport`; los 8 endpoints, 201/204/400/404/409/422); cobertura de `api.py` 100%.

- [x] **T6.6**: Tests de contrato: CLI vs API producen mismo resultado para mismos inputs
  - **Hecho cuando**: `pytest packages/finances-core/tests/contract/test_strategy_cli_api_parity.py -v` pasa.
  - **Hecho**: `pytest packages/finances-core/tests/contract/test_strategy_cli_api_parity.py -v` â†’ 5 passed: misma secuencia (create/update/get/list/versions/diff/export/delete) por CLI y por API contra bases independientes produce respuestas idÃ©nticas tras normalizar ids/timestamps; errores equivalentes (404â†”1, 204â†”0, 409â†”3, 400/422â†”2).

- [x] **T6.7**: Configurar `ruff` + `mypy --strict` en `finances-core` (ya en T0.2/T6.4 de ft_setup)
  - **Hecho cuando**: `python -m ruff check packages/finances-core` && `python -m mypy --strict packages/finances-core` → 0 errors.
  - **Hecho**: Desde `packages/finances-core`: `ruff check .` â†’ All checks passed; `ruff format --check` limpio; `mypy --strict src` â†’ Success (51 archivos), igual que en CI (`mypy --strict src`). Nota: `mypy --strict .` (incluye tests) reporta 50 errores preexistentes en 17 archivos de tests ajenos (indicators/marketdata/datasets/setup); ninguno en archivos de `ft_strategy_manager`.

## Fase 7: Documentación y Cierre

- [x] **T7.1**: Actualizar `README.md` sección "Inicio Rápido" con ejemplos `ft_strategy_manager create/get/list/update/diff`
  - **Hecho cuando**: README refleja comandos reales con ejemplos copy-pasteables.
  - **Hecho**: README: tabla de paquetes (ya no "planificado"), mapa 2.1 y "Inicio RÃ¡pido" con ejemplos copy-pasteables CLI (`create/list/get/update/diff/versions/export/delete`, exit codes) y API (`curl` a `/strategies`); secuencia CLI ejecutada de punta a punta en una raÃ­z temporal (create â†’ update --patch â†’ diff â†’ delete --purge --force) y la clave `api_key` no aparece en el log JSON.

- [x] **T7.2**: Verificar matriz Art. 20 Constitución (100% dimensiones) via checklist interno
  - **Hecho cuando**: Checklist firmado en PR description; CI pipeline verde.
  - **Hecho**: checklist Art. 20 firmado por el usuario en la descripción del PR #8 (2026-11-02). CI verde (ubuntu-24.04 y windows-latest, py3.12) confirmado por el usuario tras el rebase sobre main; PR #8 mergeado (2026-11-02).
  - *(Histórico, previo al PR #8)* Pendiente: la firma en la descripción del PR y el CI verde requieren push/PR (no realizados). Checklist Art. 20 verificado localmente: Funcional (RF-1..RF-10 con tests EARS, cobertura `ft_strategy_manager` 99%); Contractual (spec/plan/tasks actualizados); Arquitectural (paridad CLI/API en `tests/contract`; sin imports a otras `ft_*`, comprobado con grep); Datos (Pydantic en payloads, parámetros JSON nativos, timestamps `int64` ms; Art. 5 aplica a datos de mercado, no a parámetros de estrategia); Concurrencia (`run_in_threadpool`, conexión propia por llamada, `BEGIN IMMEDIATE`, WAL); Observabilidad (logs duales + auditoría con secretos enmascarados); Calidad (ruff + `mypy --strict src` limpios); Resiliencia, Desempeño y Despliegue asíncrono (Art. 18) no aplican: CRUD síncrono local sin tareas pesadas.
- [x] **T7.3**: Registrar router en `ft_api_service` main app (integración cross-package)
  - **Hecho cuando**: `ft_api_service` importa e incluye `strategy_router`; health-check incluye strategies DB.
  - **Hecho**: No existe `ft_api_service`; la raÃ­z de composiciÃ³n es `finances-api`: `create_app()` incluye `ft_strategy_manager.api.router` y `GET /health` aÃ±ade `strategies_db_accessible` (spec/plan/tasks de `specs/finances_api` actualizados, T10). `pytest packages/finances-api` â†’ 27 passed; `ruff`/`mypy --strict src` limpios.


## Fase 8: Snapshot de metadatos por versión (Deriva 10)

- [x] **T8.1**: Esquemas y persistencia: columnas `description`/`tags` en `strategy_versions`, migración idempotente con backfill de la última versión, `StrategyVersion`/`StrategyUpdate`/`DiffResult` actualizados (RF-3, RF-8)
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_db.py packages/finances-core/tests/unit/test_strategy_schemas.py -v` pasa, incluida la migración de una base creada con el esquema anterior (parámetros, hashes y versiones previas intactos).
  - **Hecho**: esquemas + migración con backfill; `test_strategy_db.py`/`test_strategy_schemas.py` verdes (migración de una base con el esquema anterior verificada, idempotente).

- [x] **T8.2**: Servicio: `create` y `update` guardan el snapshot; `update` solo-metadatos crea versión; "No changes detected" solo si parámetros y metadatos son iguales; `diff` informa `metadata_changed` (RF-3, RF-6)
  - **Hecho cuando**: `pytest packages/finances-core/tests/unit/test_strategy_service.py -v` pasa con casos de metadatos (solo description, solo tags, herencia, sin cambios, legado).
  - **Hecho**: `test_strategy_service.py` verde (create/update/diff/export con snapshot, solo-metadatos, herencia, sin cambios, auditoría).

- [x] **T8.3**: CLI/API: `update` acepta solo `--desc`/`--tag` (PUT solo con `description`/`tags`); `diff` muestra metadatos; 422/exit 2 si el payload está vacío; paridad CLI/API
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_strategy_cli.py packages/finances-core/tests/integration/test_strategy_api.py packages/finances-core/tests/contract -v` pasa.
  - **Hecho**: `test_strategy_cli.py`/`test_strategy_api.py`/contrato verdes; `--desc`/`--tag` solos y PUT solo-metadatos crean versión; payload vacío → exit 2 / 422.

- [x] **T8.4**: Documentación y cierre: README, ruff, `mypy --strict src`, suite completa; Risk item 1 (formato persistido) con verificación independiente
  - **Hecho cuando**: ruff + `mypy --strict src` limpios y suites de `finances-core`/`finances-api` verdes.
  - **Hecho**: ruff + `mypy --strict src` limpios; `pytest packages/finances-core` 675 passed, `packages/finances-api` 27 passed. Verificación independiente (Risk item 1): sin bloqueantes (migración idempotente y concurrente, sin regresiones vs HEAD); su aviso menor (diff legado→snapshot mostraba un cambio de `description` falso) corregido.
