# Tasks — `ft_market_data_manager` (MVP v1.0)

Cada tarea cierra con su **"Hecho cuando"**. Flujo: test en rojo → implementación → verde (Art. 7). Quién modifica el almacén de velas es solo esta herramienta (ADR-M01).

## Fase 1: Fundaciones

- [x] **T1.1**: Extraer el descubrimiento Hive (`_discover`, `dataset_files`, `resolve_dataset_path`, identificadores) de `ft_data_viewer/scanner.py` a `finances_core/marketdata/dataset_layout.py`; el viewer lo importa desde ahí
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer -v` pasa sin cambios de comportamiento y sin imports entre `ft_*`.
  - **Hecho**: nuevo `finances_core/marketdata/dataset_layout.py` (`discover_datasets`, `dataset_files`, `partition_key`, `find_dataset_path`, `dataset_label`, `MARKET_TYPES`) con 7 tests; `ft_data_viewer/scanner.py` lo consume y reexporta los nombres públicos (`__all__`), sin cambios de comportamiento: `finances-core` + `finances-downloader` + `finances-api` 1245 passed; ruff y `mypy --strict src` de `finances-core` limpios (el único error de mypy de `finances-downloader` es el preexistente de `pandas-stubs` en el adaptador de yfinance).

- [x] **T1.2**: Esquemas Pydantic (`schemas/market_data_manager.py`)
  - **Hecho cuando**: tests de validación (`PruneRequest` exige exactamente uno de `before_ms`/`all` y filtro de dataset con `all`; `ImportRequest` exige destino completo); ruff + `mypy --strict` limpios.
  - **Hecho**: `finances_core/schemas/market_data_manager.py` (`DatasetSelector`, `PartitionResult`, `ManagerRequest`, `PruneRequest` con validador, `ImportRequest`, `ImportPlan`, `ManagerTaskResponse`, `ManagerResult`) exportado desde `schemas/__init__.py`; `pytest packages/finances-core/tests/unit/test_market_data_manager_schemas.py`: 12 passed; ruff y `mypy --strict src` limpios.

- [x] **T1.3**: `planner.py`: `plan(selector)`, snapshot (tamaño + `mtime_ns`) y `commit()` optimista con `atomic_write`
  - **Hecho cuando**: tests con almacén temporal: selección por exchange/market_type/symbol/timeframe/rango; una partición modificada entre `plan` y `commit` devuelve `conflict` y no se toca; sobrantes se borran solo tras el reemplazo (RF-6).
  - **Hecho**: `ft_market_data_manager/planner.py` (`plan`, `Partition` con snapshot nombre/tamaño/`mtime_ns`, `commit` optimista sobre `atomic_write`); `pytest packages/finances-downloader/tests/ft_market_data_manager/test_planner.py`: 11 passed (selector exchange/market_type/symbol/timeframe/rango, `conflict` por modificación, `mtime` o archivo nuevo, sobrantes borrados solo tras el reemplazo, directorios Hive vacíos eliminados).

## Fase 2: Compactación y Deduplicación

- [x] **T2.1**: `service.run_compact` (RF-1)
  - **Hecho cuando**: test que parte de una partición con varios archivos y obtiene un `data.parquet` ordenado con las mismas filas; una partición ya compacta responde `unchanged` y no cambia su `mtime`; `--dry-run` no escribe.
  - **Hecho**: `service.run_compact` (RF-1) con auditoría RF-8; tests en `test_service.py` (varios archivos -> un `data.parquet` ordenado con las mismas filas, `unchanged` sin cambiar `mtime`, `dry_run` -> `planned` sin escribir, `float32`/`int64` intactos, partición rota -> `error` sin abortar las demás, `conflict`).

- [x] **T2.2**: `service.run_dedupe` (RF-2)
  - **Hecho cuando**: test con duplicados entre archivos: prevalece la última fila en orden de lectura; el informe lista duplicados eliminados y un ejemplo; sin duplicados → `unchanged`; esquema `float32`/`int64` intacto.
  - **Hecho**: `service.run_dedupe` (RF-2): prevalece la última fila en orden de lectura, `detail` con duplicados eliminados y un ejemplo de `open_time_ms`, sin duplicados -> `unchanged` sin tocar archivos, idempotente, dtypes intactos.

## Fase 3: Retención y Borrado

- [x] **T3.1**: `service.run_prune` (RF-3)
  - **Hecho cuando**: tests: `before_ms` borra solo meses completos anteriores y no toca el que contiene `T`; `--all` sin filtros es inválido; se eliminan los directorios Hive vacíos; sin `force`/`confirm` no se borra nada; `dry_run` devuelve el plan.
  - **Hecho**: `service.run_prune` (RF-3): `before_ms` borra meses cuyo fin <= T (el que contiene T queda), `all` exige filtro (validador de `PruneRequest`), directorios Hive vacíos eliminados, sin `confirm` y sin `dry_run` -> `ConfirmationRequiredError` sin borrar, `dry_run` -> `planned`. Cierre: `pytest packages/finances-core packages/finances-downloader packages/finances-api`: 1332 passed; `--cov=finances_downloader.ft_market_data_manager`: 95 %; ruff y `mypy --strict src` (core y downloader) limpios.

## Fase 4: CLI

- [x] **T4.1**: Typer `cli.py` con `compact`, `dedupe`, `prune` (flags globales, `--format table|json`, barra Rich, confirmación en `prune`)
  - **Hecho cuando**: `ft_market_data_manager --help` lista los comandos; tests de integración con almacén temporal cubren cada comando; códigos de salida de RF-10 (incluido el rechazo de confirmación → exit 1).
  - **Hecho**: `ft_market_data_manager/cli.py` (`compact`, `dedupe`, `prune`; flags globales `--verbose/--quiet/--log-format/--format`, selectores repetibles, `--dry-run`, `--before-ms`/`--all`, `--force`, barra Rich en stderr, confirmación `typer.confirm(abort=True)` tras mostrar el plan) y script `ft_market_data_manager` en `pyproject.toml`; `pytest packages/finances-downloader/tests/ft_market_data_manager/test_cli.py`: 26 passed (ayuda, entry point, cada comando, `--dry-run` sin escribir, confirmación rechazada -> exit 1 sin borrar, exit 2 en argumentos inválidos, `conflict`/`error` de partición -> exit 0, mapeo 1/3/4).

## Fase 5: API

- [x] **T5.1**: `api.py`: `POST /compact`, `/dedupe`, `/prune`, `GET /status/{task_id}`, `GET /result/{task_id}` con `SQLTaskManager` y `BackgroundTasks` (Art. 18)
  - **Hecho cuando**: tests con `httpx.AsyncClient`: 202 con `task_id` y `poll_url`; polling hasta `COMPLETED`; `prune` sin `confirm` → 400; `/result` de una tarea no terminada → 409; errores de partición no hacen fallar la tarea.
  - **Hecho**: `ft_market_data_manager/api.py` (`POST /compact|/dedupe|/prune`, `GET /status|/result/{task_id}`; tarea en `SQLTaskManager`, trabajo en `BackgroundTasks`/*threadpool*; `prune` sin `confirm` ni `dry_run` -> 400) y gancho opcional `on_partition` en `service.run_*` (comportamiento por defecto idéntico); `pytest .../test_api.py`: 18 passed (202 con `task_id`/`poll_url`, polling a `COMPLETED`, contadores, 400/422, `/result` no terminado -> 409, 404, `error`/`conflict` no fallan la tarea, excepción -> `FAILED` y 500 en `/result`). Contrato CLI↔API parcial de T7.2 (`test_contract.py`, compact/dedupe/prune): 7 passed.

- [x] **T5.2**: Montar el router en `finances_api` y actualizar `specs/finances_api`
  - **Hecho cuando**: `pytest packages/finances-api -v` pasa y `/openapi.json` incluye las rutas `/market-data/manager/*`.
  - **Hecho**: `create_app()` monta `ft_market_data_manager.api.router`; `specs/finances_api` (spec RF-1, plan, T11) actualizado; `pytest packages/finances-api`: 29 passed (rutas en `/openapi.json`, `poll_url` bajo `/market-data/manager`, nombres de ruta únicos). Cierre: `finances-core` + `finances-downloader` + `finances-api` 1388 passed; `--cov=finances_downloader.ft_market_data_manager`: 98 %; ruff, `ruff format` y `mypy --strict src` (core, downloader, api) limpios.

## Fase 6: Importación

- [x] **T6.1**: `importer.py`: lectura de Parquet/CSV/JSONL con esquema explícito, conversión de `--ts-column`/`--ts-unit`, plausibilidad, alineación a la grilla del timeframe y duplicados en el origen (RF-4, RF-5)
  - **Hecho cuando**: tests por regla: columnas faltantes/sobrantes → error sin escribir; segundos tomados por milisegundos → rechazo con pista; `open_time_ms` desalineado (p. ej. +3 h) → rechazo con conteo y ejemplo, sin redondear; `1d`/`1w`/`1M` con su grilla; duplicados en el origen → error; ISO-8601 no aceptado.
  - **Hecho**: `ft_market_data_manager/importer.py` (fase 1: lectura Parquet/CSV/JSONL con esquema explícito, `--ts-column`/`--ts-unit`, plausibilidad 2000-01-01..ahora+1d con pista de unidad, alineación a la grilla, duplicados, columnas faltantes/sobrantes, filas inválidas con `skip_invalid`) y `finances_core/marketdata/timeframes.py` (`is_aligned`, `aligned_expr`, `timeframe_duration_ms`); `pytest packages/finances-core/tests/unit/test_marketdata_timeframes.py`: 30 passed; `pytest .../ft_market_data_manager/test_import.py`: 64 passed (segundos como ms -> pista `--ts-unit s`, +30 min y +3 h rechazados con conteo y ejemplo sin escribir, grillas `1d`/`1w`/`1M`, ISO-8601 rechazado en CSV/JSONL/Parquet, duplicados, columnas, `us`/`ns` exactos, esquema yfinance).

- [x] **T6.2**: `service.run_import`: destino explícito (`--create`), dos fases, `--on-conflict skip|fail|replace`, divergencia de `close`, vista previa
  - **Hecho cuando**: tests: dataset inexistente sin `--create` → 404; `skip` conserva las filas existentes; `fail` aborta con conflictos; `replace` con divergencia > 1 % aborta sin `--force`; ninguna fila existente cambia de `open_time_ms`; un archivo inválido no escribe ninguna partición; `--dry-run` devuelve el `ImportPlan` sin escribir; una partición modificada en paralelo → `conflict`.
  - **Hecho**: `service.run_import` (dos fases; `planner.commit` con verificación optimista y `atomic_write`; `importer.merge_rows` propio, sin `merge_partition`) con auditoría RF-8; `test_import.py`: destino inexistente sin `--create` -> `DatasetNotFoundError`, con `--create` crea particiones, `skip` conserva las filas existentes idénticas, `fail` aborta, `replace` solo cambia valores y la divergencia > 1 % aborta sin `force`, ninguna fila existente cambia de `open_time_ms`, archivo inválido (2.º mes) deja el almacén idéntico (huella SHA-256 del árbol), `dry_run` devuelve el `ImportPlan` sin escribir, partición modificada en paralelo -> `conflict`, sin `confirm`/`force` -> `ConfirmationRequiredError`.

- [x] **T6.3**: Comando `import` en la CLI y `POST /import` en la API (confirmación en CLI, `confirm`/`dry_run` en API)
  - **Hecho cuando**: tests de integración CLI y API; el round-trip `ft_data_viewer export` → `import` en un dataset vacío devuelve las mismas filas; paridad CLI/API (T7.2).
  - **Hecho**: CLI `import` (vista previa siempre en stderr, `typer.confirm` salvo `--force`/`--dry-run`, rechazo -> exit 1) y `POST /market-data/manager/import` (400 sin `confirm`/`dry_run`, 404/400 síncronos de origen y destino, 202 + `task_id`/`poll_url`); `test_import_cli.py`: 21 passed, `test_import_api.py`: 11 passed, `test_contract_import.py`: 5 passed (secuencia dry-run y confirmada CLI vs API sobre dos almacenes independientes, mapeo de errores 2/3/1 vs 422/409/404); ida y vuelta `export` (`stream_export`) -> `import --create` parquet/csv/jsonl da las mismas filas y esquema. Derivas 1-4 en `plan.md` §3.5.

## Fase 7: Calidad, Contrato y Cierre

- [x] **T7.1**: Tests unitarios de `planner`/`service` y de integración sobre un almacén Parquet real temporal
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_market_data_manager -v` pasa; cobertura ≥ 90%.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_market_data_manager` → 235 passed junto con `finances-api` (planner, servicio, importador, CLI y API sobre almacenes Parquet temporales reales); cobertura del manager 98 % (`--cov=finances_downloader.ft_market_data_manager`).

- [x] **T7.2**: Test de contrato CLI ↔ API: la misma secuencia produce resultados equivalentes tras normalizar ids y timestamps
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_market_data_manager/test_contract.py -v` pasa.
  - **Hecho**: `test_contract.py` (7) y `test_contract_import.py` (5): la misma secuencia por CLI (JSON) y por API contra dos almacenes independientes da resultados equivalentes tras normalizar ids y timestamps; errores equivalentes (2↔400/422, confirmación faltante, no encontrado 1↔404).

- [x] **T7.3**: Test de aislamiento: ninguna `ft_*` importa de `ft_market_data_manager` ni viceversa; el viewer sigue sin escribir
  - **Hecho cuando**: el test de imports pasa y el viewer no usa ninguna función de escritura.
  - **Hecho**: `test_isolation.py` (13 tests AST): el manager solo importa de `finances_core` (sin `ft_*` hermanas, `finances_analytics` ni `finances_api`); ninguna otra `ft_*` importa del manager; `ft_data_viewer` no usa los escritores del almacén (`atomic_write`, `write_parquet_partitioned`, `hive_writer`).

- [x] **T7.4**: Documentación: README (tabla de paquetes y mapa 2.2), fila "Modificación de datos" de la spec del viewer, `specs/finances_api`
  - **Hecho cuando**: README con ejemplos copy-pasteables de `compact`, `dedupe`, `prune --dry-run` e `import --dry-run`; sin referencias a "planificado" para el manager.
  - **Hecho**: README con ejemplos copy-pasteables de `compact`, `dedupe`, `prune --dry-run` e `import --dry-run` y el manager descrito como implementado; fila "Modificación de datos" de la spec del viewer apuntando al manager; `specs/finances_api` actualizada (T11).

- [x] **T7.5**: Matriz Art. 20 (100% dimensiones) en la descripción del PR; ruff + `ruff format` + `mypy --strict src` + pytest verdes en CI
  - **Hecho cuando**: checklist firmado en el PR y CI verde.
  - **Hecho**: PR #13 (y #12 del que depende) con la matriz Art. 20 firmada por el usuario y CI verde en ubuntu-24.04 y windows-latest; mergeados el 2026-11-02. Dos verificaciones independientes sin bloqueantes. Un fallo ajeno del CI (`finances-analytics`, latencia del 202) se resolvió en `b6e18ea` dentro del PR #12.
