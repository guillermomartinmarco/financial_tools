# Tasks — `ft_market_data_manager` (MVP v1.0)

Cada tarea cierra con su **"Hecho cuando"**. Flujo: test en rojo → implementación → verde (Art. 7). Quién modifica el almacén de velas es solo esta herramienta (ADR-M01).

## Fase 1: Fundaciones

- [x] **T1.1**: Extraer el descubrimiento Hive (`_discover`, `dataset_files`, `resolve_dataset_path`, identificadores) de `ft_data_viewer/scanner.py` a `finances_core/marketdata/dataset_layout.py`; el viewer lo importa desde ahí
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer -v` pasa sin cambios de comportamiento y sin imports entre `ft_*`.
  - **Hecho**: nuevo `finances_core/marketdata/dataset_layout.py` (`discover_datasets`, `dataset_files`, `partition_key`, `find_dataset_path`, `dataset_label`, `MARKET_TYPES`) con 7 tests; `ft_data_viewer/scanner.py` lo consume y reexporta los nombres públicos (`__all__`), sin cambios de comportamiento: `finances-core` + `finances-downloader` + `finances-api` 1245 passed; ruff y `mypy --strict src` de `finances-core` limpios (el único error de mypy de `finances-downloader` es el preexistente de `pandas-stubs` en el adaptador de yfinance).

- [ ] **T1.2**: Esquemas Pydantic (`schemas/market_data_manager.py`)
  - **Hecho cuando**: tests de validación (`PruneRequest` exige exactamente uno de `before_ms`/`all` y filtro de dataset con `all`; `ImportRequest` exige destino completo); ruff + `mypy --strict` limpios.

- [ ] **T1.3**: `planner.py`: `plan(selector)`, snapshot (tamaño + `mtime_ns`) y `commit()` optimista con `atomic_write`
  - **Hecho cuando**: tests con almacén temporal: selección por exchange/market_type/symbol/timeframe/rango; una partición modificada entre `plan` y `commit` devuelve `conflict` y no se toca; sobrantes se borran solo tras el reemplazo (RF-6).

## Fase 2: Compactación y Deduplicación

- [ ] **T2.1**: `service.run_compact` (RF-1)
  - **Hecho cuando**: test que parte de una partición con varios archivos y obtiene un `data.parquet` ordenado con las mismas filas; una partición ya compacta responde `unchanged` y no cambia su `mtime`; `--dry-run` no escribe.

- [ ] **T2.2**: `service.run_dedupe` (RF-2)
  - **Hecho cuando**: test con duplicados entre archivos: prevalece la última fila en orden de lectura; el informe lista duplicados eliminados y un ejemplo; sin duplicados → `unchanged`; esquema `float32`/`int64` intacto.

## Fase 3: Retención y Borrado

- [ ] **T3.1**: `service.run_prune` (RF-3)
  - **Hecho cuando**: tests: `before_ms` borra solo meses completos anteriores y no toca el que contiene `T`; `--all` sin filtros es inválido; se eliminan los directorios Hive vacíos; sin `force`/`confirm` no se borra nada; `dry_run` devuelve el plan.

## Fase 4: CLI

- [ ] **T4.1**: Typer `cli.py` con `compact`, `dedupe`, `prune` (flags globales, `--format table|json`, barra Rich, confirmación en `prune`)
  - **Hecho cuando**: `ft_market_data_manager --help` lista los comandos; tests de integración con almacén temporal cubren cada comando; códigos de salida de RF-10 (incluido el rechazo de confirmación → exit 1).

## Fase 5: API

- [ ] **T5.1**: `api.py`: `POST /compact`, `/dedupe`, `/prune`, `GET /status/{task_id}`, `GET /result/{task_id}` con `SQLTaskManager` y `BackgroundTasks` (Art. 18)
  - **Hecho cuando**: tests con `httpx.AsyncClient`: 202 con `task_id` y `poll_url`; polling hasta `COMPLETED`; `prune` sin `confirm` → 400; `/result` de una tarea no terminada → 409; errores de partición no hacen fallar la tarea.

- [ ] **T5.2**: Montar el router en `finances_api` y actualizar `specs/finances_api`
  - **Hecho cuando**: `pytest packages/finances-api -v` pasa y `/openapi.json` incluye las rutas `/market-data/manager/*`.

## Fase 6: Importación

- [ ] **T6.1**: `importer.py`: lectura de Parquet/CSV/JSONL con esquema explícito, conversión de `--ts-column`/`--ts-unit`, plausibilidad, alineación a la grilla del timeframe y duplicados en el origen (RF-4, RF-5)
  - **Hecho cuando**: tests por regla: columnas faltantes/sobrantes → error sin escribir; segundos tomados por milisegundos → rechazo con pista; `open_time_ms` desalineado (p. ej. +3 h) → rechazo con conteo y ejemplo, sin redondear; `1d`/`1w`/`1M` con su grilla; duplicados en el origen → error; ISO-8601 no aceptado.

- [ ] **T6.2**: `service.run_import`: destino explícito (`--create`), dos fases, `--on-conflict skip|fail|replace`, divergencia de `close`, vista previa
  - **Hecho cuando**: tests: dataset inexistente sin `--create` → 404; `skip` conserva las filas existentes; `fail` aborta con conflictos; `replace` con divergencia > 1 % aborta sin `--force`; ninguna fila existente cambia de `open_time_ms`; un archivo inválido no escribe ninguna partición; `--dry-run` devuelve el `ImportPlan` sin escribir; una partición modificada en paralelo → `conflict`.

- [ ] **T6.3**: Comando `import` en la CLI y `POST /import` en la API (confirmación en CLI, `confirm`/`dry_run` en API)
  - **Hecho cuando**: tests de integración CLI y API; el round-trip `ft_data_viewer export` → `import` en un dataset vacío devuelve las mismas filas; paridad CLI/API (T7.2).

## Fase 7: Calidad, Contrato y Cierre

- [ ] **T7.1**: Tests unitarios de `planner`/`service` y de integración sobre un almacén Parquet real temporal
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_market_data_manager -v` pasa; cobertura ≥ 90%.

- [ ] **T7.2**: Test de contrato CLI ↔ API: la misma secuencia produce resultados equivalentes tras normalizar ids y timestamps
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_market_data_manager/test_contract.py -v` pasa.

- [ ] **T7.3**: Test de aislamiento: ninguna `ft_*` importa de `ft_market_data_manager` ni viceversa; el viewer sigue sin escribir
  - **Hecho cuando**: el test de imports pasa y el viewer no usa ninguna función de escritura.

- [ ] **T7.4**: Documentación: README (tabla de paquetes y mapa 2.2), fila "Modificación de datos" de la spec del viewer, `specs/finances_api`
  - **Hecho cuando**: README con ejemplos copy-pasteables de `compact`, `dedupe`, `prune --dry-run` e `import --dry-run`; sin referencias a "planificado" para el manager.

- [ ] **T7.5**: Matriz Art. 20 (100% dimensiones) en la descripción del PR; ruff + `ruff format` + `mypy --strict src` + pytest verdes en CI
  - **Hecho cuando**: checklist firmado en el PR y CI verde.
