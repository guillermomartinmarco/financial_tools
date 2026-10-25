# Tasks — `ft_market_data_binance` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).

## Fase 0: Fundación del Paquete `finances-downloader`

- [ ] **T0.1**: Crear `packages/finances-downloader/pyproject.toml` con deps base + opcionales dev
  - **Hecho cuando**: `uv sync --package finances-downloader` resuelve sin conflictos; `uv run ruff check packages/finances-downloader` pasa.
- [ ] **T0.2**: Crear `packages/finances-downloader/src/finances_downloader/__init__.py` + estructura de carpetas
  - **Hecho cuando**: `from finances_downloader.ft_market_data_binance import cli` importa sin errores.
- [ ] **T0.3**: Añadir modelos Pydantic a `packages/finances-core/src/finances_core/schemas/market_data.py` (DownloadRequest, DownloadPlan, DownloadPlanItem, DownloadProgress, DownloadResult, OHLCV_SCHEMA, OHLCV_PARTITION_COLS)
  - **Hecho cuando**: Modelos validan ejemplos JSON; MyPy strict pasa sin errors.
- [ ] **T0.4**: Extender `FinancesSettings` en `packages/finances-core/src/finances_core/config.py` con 12 nuevas variables `FT_BINANCE_*` y `FT_DOWNLOAD_MAX_CONCURRENT`
  - **Hecho cuando**: `FinancesSettings()` instancia con defaults; `model_config.env_prefix = "FT_"`.
- [ ] **T0.5**: Actualizar `packages/finances-core/src/ft_setup/templates/env_list.conf.j2` con las 12 nuevas variables (metadatos: source_tool="ft_market_data_binance", since_version="0.1.0")
  - **Hecho cuando**: `ft_setup doctor` muestra las nuevas vars; `ft_setup init` regenera `.env.template` con ellas.

## Fase 1: Cliente HTTP Dual + Token Bucket

- [ ] **T1.1**: Implementar `TokenBucket` genérico en `packages/finances-downloader/src/ft_market_data_binance/rate_limiter.py`
  - **Hecho cuando**: Test unitario verifica: capacidad, refill rate, `acquire()` bloquea cuando vacío, tokens se recargan por tiempo.
- [ ] **T1.2**: Implementar `BinancePublicDataClient` en `client.py` — base_url, rate_limiter generoso, `HEAD` para verificar existencia, `GET` streaming para descargar .zip
  - **Hecho cuando**: Test de integración (mock HTTP) verifica: construye URLs mensuales correctas, respeta rate limit, maneja 404/200, streaming download a archivo temporal.
- [ ] **T1.3**: Implementar `BinanceRestClient` en `client.py` — base_url, rate_limiter estricto, `GET /klines` con paginación, retry con backoff exponencial + jitter (tenacity)
  - **Hecho cuando**: Test de integración (mock HTTP) verifica: paginación correcta, headers rate limit, retry en 429/5xx, backoff exponencial, agota retries → excepción.
- [ ] **T1.4**: Implementar `checksum_sha256(file_path)` y validación en `client.py`
  - **Hecho cuando**: Test verifica: checksum coincide con contenido conocido; mismatch lanza excepción.

## Fase 2: Planificador (Pasos 1-6 del Pipeline)

- [ ] **T2.1**: Implementar `parse_time_input(s: str) -> int` en `planner.py`
  - **Hecho cuando**: Tests: "2024-01-01" → 1704067200000, "1704067200000" → 1704067200000, "now" → timestamp actual ms, ISO con TZ → UTC ms.
- [ ] **T2.2**: Implementar `inventory_local_parquet(parquet_dir, symbol, tf, market) -> List[Tuple[int,int]]` en `inventory.py`
  - **Hecho cuando**: Test con directorio Parquet temporal particionado: retorna rangos correctos; usa `pl.scan_parquet(...).select(pl.col("open_time_ms").min(), pl.col("open_time_ms").max()).collect()` sin leer datos completos.
- [ ] **T2.3**: Implementar `compute_gaps(req_start, req_end, local_ranges) -> List[Tuple[int,int]]` en `inventory.py`
  - **Hecho cuando**: Tests: sin locales → gap completo; locales cubren todo → lista vacía; locales parciales → gaps correctos; ranges adyacentes/overlap → merge correcto.
- [ ] **T2.4**: Implementar `check_public_data_availability(gaps, symbol, tf, market, client) -> Tuple[List[PlanItem], List[Tuple[int,int]]]` en `public_data.py`
  - **Hecho cuando**: Test (mock client): para cada gap, genera URLs mensuales correctas; HEAD 200 → PlanItem public_data; HEAD 404 → gap residual; respeta rate limit.
- [ ] **T2.5**: Implementar `plan_rest_fallback(uncovered_gaps, symbol, tf, market, limit, client) -> List[PlanItem]` en `rest_api.py`
  - **Hecho cuando**: Test: gap de 5000 velas con limit=1000 → 5 PlanItems REST con startTime/endTime correctos; gap < limit → 1 PlanItem.
- [ ] **T2.6**: Implementar `generate_download_plan(request: DownloadRequest) -> DownloadPlan` en `planner.py` (orquesta pasos 1-6)
  - **Hecho cuando**: Test de integración end-to-end (mock clients + FS temporal): request válida → plan con items public_data + rest_api, task_id UUID v7, covers_range_ms correctos, archivo JSON escrito en temp.

## Fase 3: Normalización y Escritura Parquet

- [ ] **T3.1**: Implementar `normalize_binance_klines(df: pl.DataFrame, source: Literal["public_data","rest_api"]) -> pl.DataFrame` en `normalizer.py`
  - **Hecho cuando**: Tests:
    - Public Data CSV: 12 columnas → esquema canónico 13 columnas (añade symbol, tf, market, year, month); dtypes correctos; dedup por open_time_ms; ordenado.
    - REST JSON: array de 12 elementos → mismo resultado.
    - Validación: filas fuera de rango filtradas; schema Arrow coincide con OHLCV_SCHEMA.
- [ ] **T3.2**: Implementar `write_parquet_partitioned(df: pl.DataFrame, parquet_dir: Path) -> List[str]` en `writer.py`
  - **Hecho cuando**: Test: escribe en estructura Hive correcta; `partition_by` funciona; compresión zstd; statistics=True; retorna lista de rutas; re-ejecutar sobrescribe partición (idempotencia).
- [ ] **T3.3**: Test de idempotencia: misma descarga 2 veces → mismo número de archivos, mismo row count, mismos hashes
  - **Hecho cuando**: Test integra normalizer + writer + inventory; 2da ejecución no duplica rows.

## Fase 4: Orquestador Principal (Paso 7)

- [ ] **T4.1**: Implementar `execute_download_plan(plan: DownloadPlan, settings, semaphore) -> DownloadResult` en `service.py`
  - **Hecho cuando**: Test (mock clients): procesa items en paralelo con semáforo; actualiza estados en SQLTaskManager; agrupa DataFrames por (symbol, tf, market); normaliza; escribe Parquet; retorna DownloadResult con stats.
- [ ] **T4.2**: Integrar `SQLTaskManager` para persistir progreso por fase (PLANNING, DOWNLOADING_PUBLIC, DOWNLOADING_REST, NORMALIZING, WRITING_PARQUET, COMPLETED/FAILED)
  - **Hecho cuando**: Test: cada transición de estado persiste en `share/tasks.db`; campos `downloaded_bytes`, `processed_candles`, `gaps_*` actualizados; `errors_json` captura errores por item.
- [ ] **T4.3**: Manejo de errores por item: fallo en un item no detiene los demás; se registra en `errors` y continúa
  - **Hecho cuando**: Test: 1 item falla (404 Public Data + REST agotado), otros 3 succeed → result.status=COMPLETED, errors contiene 1 entrada, gaps_unfillable poblado.

## Fase 5: CLI (Typer)

- [ ] **T5.1**: Implementar `cli.py` con comandos `download`, `plan`, `status` + callback logging
  - **Hecho cuando**: `uv run finances-downloader binance --help` muestra 3 subcomandos; help descriptivo.
- [ ] **T5.2**: `download`: parse args → DownloadRequest → genera plan → si `--dry-run` imprime plan y sale; sino ejecuta `execute_download_plan` con progress bars Rich
  - **Hecho cuando**: Test CLI (temp dir, mock clients): `--dry-run` imprime JSON plan; sin dry-run muestra progress bars por fase; tabla final con stats por símbolo.
- [ ] **T5.3**: `plan <task_id>`: lee `download_plan_{task_id}.json` y muestra (json/yaml)
  - **Hecho cuando**: Test: archivo existe → imprime; no existe → error claro.
- [ ] **T5.4**: `status <task_id>`: consulta SQLTaskManager y muestra progreso
  - **Hecho cuando**: Test: task existe → muestra fase, contadores; no existe → error.
- [ ] **T5.5**: Flags globales: `--verbose/-v`, `--quiet/-q`, `--log-format [rich|json]`, `--max-concurrent` (sobrescribe env)
  - **Hecho cuando**: Tests verifican cada flag afecta comportamiento; `--max-concurrent` pasa a `DownloadRequest.max_concurrent`.
- [ ] **T5.6**: Exit codes: `0` OK, `1` error genérico, `2` validación entrada, `3` error descarga (parcial), `4` IO/permiso
  - **Hecho cuando**: Tests `pytest -k cli_exit_codes` pasan.

## Fase 6: API (FastAPI)

- [ ] **T6.1**: Implementar `api.py` con router `/market-data/binance` y 4 endpoints
  - **Hecho cuando**: `ft_serve` (paquete `finances-api`) levanta; OpenAPI docs en `/docs` muestran endpoints.
- [ ] **T6.2**: `POST /download` → valida → genera plan → `BackgroundTasks.add_task(execute_download_plan)` → retorna `202` con `task_id`, `poll_url`
  - **Hecho cuando**: Test: response schema correcto; task_id UUID v7; poll_url absoluta; tarea se ejecuta en background.
- [ ] **T6.3**: `GET /status/{task_id}` → retorna `DownloadProgress` actual
  - **Hecho cuando**: Test polling: POST → GET status (PENDING→PLANNING→...) → COMPLETED.
- [ ] **T6.4**: `GET /plan/{task_id}` → retorna `DownloadPlan` (archivo JSON)
  - **Hecho cuando**: Test: plan existe → 200 + JSON; no existe → 404.
- [ ] **T6.5**: `GET /result/{task_id}` → retorna `DownloadResult` final
  - **Hecho cuando**: Test: task COMPLETED → 200 + parquet_paths + stats; task FAILED → 200 + errors.

## Fase 7: Tests y Calidad (Art. 7, 17 Constitución)

- [ ] **T7.1**: Tests unitarios: `rate_limiter.py`, `inventory.py`, `normalizer.py`, `planner.py` (funciones puras)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/unit/ -v` pasa; cobertura ≥ 95%.
- [ ] **T7.2**: Tests de integración: `client.py` (mock HTTP), `public_data.py`, `rest_api.py`, `writer.py` (FS real temp)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/ -v` pasa.
- [ ] **T7.3**: Tests de integración CLI: todos los comandos con temp dir, mock clients
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/test_cli.py -v` pasa.
- [ ] **T7.4**: Tests de integración API: todos los endpoints con `httpx.AsyncClient` (testserver)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/test_api.py -v` pasa.
- [ ] **T7.5**: Tests de contrato: CLI vs API producen mismo resultado para mismos inputs
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/contract/ -v` pasa.
- [ ] **T7.6**: Configurar `ruff` + `mypy --strict` en `finances-downloader`; `pre-commit` hooks
  - **Hecho cuando**: `uv run ruff check packages/finances-downloader` && `uv run mypy --strict packages/finances-downloader` → 0 errors.

## Fase 8: Documentación y Cierre

- [ ] **T8.1**: Actualizar `README.md` sección "Inicio Rápido" con ejemplos `ft_market_data_binance download --symbol BTCUSDT --timeframe 1h --start 2024-01-01`
  - **Hecho cuando**: README refleja comandos reales con ejemplos copy-pasteables.
- [ ] **T8.2**: Verificar matriz Art. 20 Constitución (100% dimensiones) via checklist interno
  - **Hecho cuando**: Checklist firmado en PR description; CI pipeline verde.
- [ ] **T8.3**: Registrar router en `ft_api_service` main app
  - **Hecho cuando**: `ft_api_service` importa e incluye `binance_router`; health-check incluye parquet dir accesible.
- [ ] **T8.4**: Validar que `ft_setup doctor` detecta y lista las 12 nuevas variables `FT_BINANCE_*` y `FT_DOWNLOAD_MAX_CONCURRENT`
  - **Hecho cuando**: `ft_setup doctor` output incluye todas con `source_tool=ft_market_data_binance`.