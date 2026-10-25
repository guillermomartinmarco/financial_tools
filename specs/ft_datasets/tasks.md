# Tasks — `ft_datasets` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` (ver CI).
> Tests del núcleo en `packages/finances-core/tests/unit/`; de la herramienta en `packages/finances-downloader/tests/ft_datasets/`; sin red, sin subprocesos de descarga reales y con `FT_PROJECT_ROOT` temporal, salvo los marcados `real_data`.
> **Antes de codificar**: consultar Context7 para Polars, Typer, FastAPI y Pydantic (AGENTS.md §5).

## Fase 0: Fundación

- [ ] **T0.1**: Marcador `real_data` en `finances-core`, `finances-downloader` y `finances-analytics` (`markers` + `addopts` con `-m 'not real_data'`, conservando `not benchmark`)
  - **Hecho cuando**: un test `real_data` de prueba no se ejecuta con `pytest -q` y sí con `pytest -m real_data` en los tres paquetes; CI por defecto sin cambios de resultado.
- [ ] **T0.2**: Esqueleto `finances_downloader/ft_datasets/` (`__init__`, `cli`, `api.router`, `py.typed` heredado) y script `ft_datasets` en `pyproject.toml`
  - **Hecho cuando**: `ft_datasets --help` responde y `test_foundation.py` importa `cli` y `api.router`.
- [ ] **T0.3**: Aislamiento Art. 4 (test AST)
  - **Hecho cuando**: el test falla si algún módulo de `ft_datasets` importa `finances_downloader.ft_*` (incluido relativo), `finances_analytics` o `finances_api`, y si `finances_core.datasets` importa `finances_downloader`; todos los módulos pasan.

## Fase 1: Contratos y configuración (núcleo)

- [ ] **T1.1**: `schemas/datasets.py`: `RecipeBlock`, `DatasetRecipe`, `RecipeItem` (`items()`, `key`), validadores RF-1/RF-2 por exchange
  - **Hecho cuando**: tests: `benchmark-v1` de plan §1.5 valida y expande a 7 ítems con claves estables; rechazos con mensaje: `exchange` desconocido, `market_type` inválido por exchange, `yfinance` con `1h`, `start ≥ end`, claves duplicadas, campo extra, `name` inválido, `schema_version ≠ 1`.
- [ ] **T1.2**: `ItemLock`, `RecipeLock`, `Probe`, `ItemVerification`, `VerifyReport` (+ `to_markdown`), `LockResult`, contratos de tarea (`MaterializePlan`, `DownloadBlock`, `BlockRun`, `MaterializeProgress`, `MaterializeResult`)
  - **Hecho cuando**: tests de *round-trip* JSON; `hash_algorithm ≠ "ftds-v1"` y `lock_version ≠ 1` rechazados; `to_markdown` produce una fila por ítem con estado y deriva.
- [ ] **T1.3**: Settings `DATASETS_*` en `config.py` y `require_datasets_dir`
  - **Hecho cuando**: tests de defaults resueltos contra la raíz del proyecto, override con `FT_DATASETS_DIR`, y `ConfigError` si `DATASETS_DIR` anida con `PARQUET_DIR` en cualquier sentido.
- [ ] **T1.4**: `datasets/recipes.py`: `load_recipe`, `list_recipes`, `validate_reproducible`, `lock_path`, `load_lock`, `write_lock` atómico, `canonical_sha256`
  - **Hecho cuando**: tests: `name` del TOML distinto del archivo → error; `../x` y rutas absolutas rechazadas; `end` dentro del margen de asentamiento → error (reloj inyectado); `canonical_sha256` no cambia al reordenar bloques, añadir comentarios o cambiar `description`, y sí al cambiar un rango; fallo simulado de `replace` conserva el *lock* previo; el JSON escrito es estable (dos escrituras = bytes idénticos).

## Fase 2: Hash y verificación (núcleo)

- [ ] **T2.1**: `datasets/hashing.py` — algoritmo `ftds-v1` (plan §3.2) en *streaming* por mes
  - **Hecho cuando**: vectores dorados con hex fijado en el test para un `DataFrame` con `-0.0`, subnormal, `±inf`, `float32::MAX`, NaN `0x7FC00000` y `0xFFC00000` (mismo hash), y nulos; `0.0` ≠ `-0.0`; cambiar un bit de un valor cambia `content_hash`, su columna y su mes; procesar por meses == procesar de una vez; el test pasa idéntico en Windows y Ubuntu (matriz de `ci.yml`).
- [ ] **T2.2**: Independencia del escritor
  - **Hecho cuando**: test escribe el mismo `DataFrame` con `zstd`, `snappy` y sin compresión, `row_group_size` 1.000 y 100.000 y con/sin estadísticas → bytes de archivo distintos, hash idéntico.
- [ ] **T2.3**: `datasets/store.py`: `scan_item` (solo meses que solapan, filtro `[start, end)`, validación de dtypes y orden) y `delete_item_partitions`
  - **Hecho cuando**: tests con almacén Hive sintético (binance y yfinance): lee solo los meses necesarios (*spy* sobre lectura), excluye la vela en `end_ms`, `open_time_ms` duplicado o dtype no canónico → error de datos; `delete_item_partitions` no borra nada fuera de `datasets_dir` ni meses no solapados.
- [ ] **T2.4**: `datasets/verify.py`: `compute_item_lock` (sondas, `missing_candles`) y `verify_recipe` con estados RF-5 y clasificación RF-6
  - **Hecho cuando**: tests: `OK`; `adj_close` cambiado → `OK_WITH_WARNINGS`; sin filas → `MISSING`; sin *lock* → `UNLOCKED`; fila quitada → `ROWS_CHANGED`; OHLC × 0,25 → `SCALE_RESTATED` con factor 0,25; un `close` alterado en marzo → `VALUES_CHANGED` con `columns_changed=["close"]` y `months_changed=["2024-03"]`; receta editada → `RECIPE_CHANGED`; `missing_candles` = 0 en un trimestre `1h` completo y = 3 tras quitar 3 velas.
- [ ] **T2.5**: `datasets/testing.py`: `require_dataset`
  - **Hecho cuando**: tests: ítem `OK` devuelve `LazyFrame` del rango; ítem `MISMATCH`/`MISSING` → `pytest.fail` con el detalle; `import finances_core.datasets` no importa `pytest`.

## Fase 3: Materialización (herramienta)

- [ ] **T3.1**: `runner.py`: resolución del ejecutable (`sysconfig` scripts → `PATH`), construcción de `argv`, entorno del hijo, *timeout*, cola de `stderr` y `downloader_task_id`
  - **Hecho cuando**: tests unitarios con un ejecutable falso (script Python) cubren exit 0/≠0, *timeout*, ejecutable ausente y extracción de `task_id` de logs JSON; **test de contrato** (sin red) ejecuta `ft_market_data_binance download --help` y `ft_market_data_yfinance download --help` y comprueba que existen todos los flags que `ft_datasets` usa.
- [ ] **T3.2**: `materializer.py`: plan (ítems omitidos, bloques reducidos, `--refetch`), persistido con prefijo `datasets_plan`
  - **Hecho cuando**: tests: con todo `OK` → plan sin bloques; un símbolo pendiente → bloque solo con ese símbolo y sus timeframes; `end_ms_inclusive = end_ms − 1`; `MISMATCH` + `refetch` → particiones borradas y bloque planificado; plan recuperable con `load_plan(prefix="datasets_plan")`.
- [ ] **T3.3**: `service.py`: tarea con fases RF-8 sobre `TaskRecorder` y `Runner` inyectable
  - **Hecho cuando**: tests con `FakeRunner` (escribe Parquet sintético en `FT_PARQUET_DIR` del entorno recibido): `COMPLETED`/`OK`; segunda ejecución sin invocar al runner (idempotencia, Art. 12); exit ≠ 0 → `FAILED`/`DOWNLOAD_FAILED` con `stderr_tail` y datos previos intactos; símbolo sin datos → `COMPLETED` con ítem `MISSING`; fases solo avanzan; el runner recibe `FT_PARQUET_DIR = FT_DATASETS_DIR` absoluto; nunca se escribe el *lock*.
- [ ] **T3.4**: `lock` en el servicio: `WRITTEN`/`UNCHANGED`/`REJECTED`/`UPDATED` (RF-7)
  - **Hecho cuando**: tests: primer *lock* escrito; repetición → `UNCHANGED` sin reescribir (mtime intacto); datos cambiados → `REJECTED` con diferencias; `--update` → `UPDATED` con diff y log `warning`; ítem `MISSING` → no se escribe nada.

## Fase 4: CLI y API

- [ ] **T4.1**: `cli.py`: `list`, `show`, `materialize` (`--no-verify`, `--refetch`, `--dry-run`), `verify` (`--format`), `lock` (`--update`), `status`, `plan`, `result`; flags globales; exit codes 0–6
  - **Hecho cuando**: tests `CliRunner` por comando y por cada exit code (0/1/2/3/4/5/6).
- [ ] **T4.2**: `api.py`: rutas de plan §2.2 con nombres únicos; `POST /materialize` → `202` inmediato; `verify`/`lock` en *threadpool*
  - **Hecho cuando**: tests `TestClient`: 202 con tarea `PENDING`, 400, 404, 409 (`result` sin terminar y `lock` rechazado), 422 por `name`/`task_id` inválidos; `name="../x"` nunca lee fuera de `FT_DATASETS_RECIPES_DIR`.
- [ ] **T4.3**: Contrato CLI ≡ API
  - **Hecho cuando**: test compara JSON de CLI y API para `show`, `verify`, `lock`, `status`, `plan`, `result`.

## Fase 5: Integración con la suite

- [ ] **T5.1**: `finances_api` monta `/datasets`
  - **Hecho cuando**: test de OpenAPI incluye las 8 rutas; `poll_url`/`plan_url`/`result_url` del `202` apuntan a `/datasets/...` en la app compuesta; spec/plan/tasks de `finances_api` actualizados.
- [ ] **T5.2**: `ft_setup`: variables `FT_DATASETS_*` en `env_list.conf(.j2)` con `source_tool="ft_datasets"`; `init` crea `DATASETS_DIR`
  - **Hecho cuando**: `test_doctor_lists_tool_variables_with_source_tool[ft_datasets]`, `test_bundled_env_list_matches_template` y un test de `init` que crea `data/datasets` pasan; registrado en `specs/ft_setup/tasks.md`.
- [ ] **T5.3**: Receta `datasets/benchmark-v1.toml` (plan §1.5)
  - **Hecho cuando**: `ft_datasets show benchmark-v1` lista 7 ítems y un test de la suite por defecto valida todas las recetas de `datasets/` (sin red).
- [ ] **T5.4**: Tests `real_data`: `ft_datasets/real_data/test_benchmark_v1.py` (todos los ítems `OK` vía `require_dataset`; BTCUSDT `1m` sin huecos inesperados frente a `missing_candles`) y `finances-analytics/tests/real_data/test_indicators_real.py` (`ft_indicators` sobre BTCUSDT `1m` y AAPL `1d` reales, contraste TA-Lib con las tolerancias de su plan)
  - **Hecho cuando**: con el almacén materializado, `pytest -m real_data` pasa en local; sin almacén, falla con mensaje claro (no se omite).
- [ ] **T5.5**: Workflow `.github/workflows/real-data.yml` (plan §3.8)
  - **Hecho cuando**: ejecución manual en `ubuntu-24.04` (misma imagen que `ci.yml`) con caché fría descarga, verifica, guarda la caché y pasa; segunda ejecución con caché caliente no lanza subprocesos de descarga (`blocks=[]` en el resultado) y termina en < 5 min; el resumen del job muestra la tabla de verificación; el workflow por defecto `ci.yml` no cambia de duración ni usa red.
- [ ] **T5.7**: Issue automático ante fallo semanal (RF-10 bis, plan §3.8.1): `scripts/ci/real_data_issue.sh`, paso `if: failure() && github.event_name == 'schedule'`, `permissions: {contents: read, issues: write}`
  - **Hecho cuando**: probado en un fork o rama con un disparador `schedule` temporal (o `workflow_dispatch` con una entrada `simulate_schedule` usada solo para la prueba): un fallo `drift` provocado (lock alterado) abre `[real-data] drift: benchmark-v1` con etiquetas `real-data` y `real-data:drift` y cuerpo con la tabla de verify (dataset, meses, deriva y factor); una segunda ejecución fallida del mismo tipo **comenta** ese issue sin abrir otro; un fallo `download` (URL base de Binance inválida) abre un issue distinto; una ejecución manual fallida no abre ni comenta nada; con el issue cerrado, el siguiente fallo abre uno nuevo. Evidencias (URLs de issues/comentarios) registradas aquí y la entrada de prueba eliminada.
- [ ] **T5.6**: README (§2.2 utilidad, §3 ejemplos copy-pasteables, servicio `/datasets`, §4 `pytest -m real_data` y etiqueta `real-data`)
  - **Hecho cuando**: los ejemplos del README se ejecutan tal cual en el smoke T6.1.

## Fase 6: Cierre

- [ ] **T6.1**: Smoke real en Windows (fuera de CI, `FT_PROJECT_ROOT` temporal): `materialize benchmark-v1` → `UNLOCKED` (exit 6) → `lock` → `verify` `OK`; segunda `materialize` sin red; `ft_data_viewer list` con `FT_PARQUET_DIR=data/datasets`; `ft_serve` expone `/datasets`; tiempo de `verify` registrado (RF-5 ≤ 5 s); `benchmark-v1.lock.json` commiteado
  - **Hecho cuando**: salida del smoke registrada en este archivo y el *lock* generado en Windows verifica `OK` en la primera ejecución del workflow en `ubuntu-24.04` (portabilidad real entre SO).
- [ ] **T6.2**: Medición de estabilidad de fuente (riesgo de plan §3.2): un mismo mes de BTCUSDT `1m` descargado por Public Data y por REST (almacenes separados; REST forzado apuntando `FT_BINANCE_PUBLIC_DATA_BASE_URL_SPOT` a una URL sin archivos, de modo que el planificador use el *fallback*; si eso no lo fuerza, se registra y se compara normalizando el CSV mensual y la respuesta REST del mismo mes con un script fuera del repo)
  - **Hecho cuando**: hashes comparados y resultado registrado aquí; si difieren, se abre una tarea en `specs/ft_market_data_binance/tasks.md` para unificar la conversión texto→`Float32` y se documenta en ADR-D02.
- [ ] **T6.3**: Verificar matriz Art. 20 vía checklist del PR (Resiliencia: delegada + *timeout* por bloque; Desempeño: RF-5 medido, sin benchmark de CI justificado)
  - **Hecho cuando**: checklist firmado en la descripción del PR; CI verde (`ci.yml` en Windows y Ubuntu y `real-data.yml` manual).
