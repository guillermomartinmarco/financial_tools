# Tasks — `ft_market_data_binance` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).

## Fase 0: Fundación del Paquete `finances-downloader`

- [ ] **T0.1**: Crear `packages/finances-downloader/pyproject.toml` con deps base + opcionales dev
  - **Hecho cuando**: `uv sync --package finances-downloader` resuelve sin conflictos; `uv run ruff check packages/finances-downloader` pasa.
  - **Pendiente**: pyproject.toml fuera del alcance de este refactor; no verificado `uv sync`.
- [x] **T0.2**: Crear `packages/finances-downloader/src/finances_downloader/__init__.py` + estructura de carpetas
  - **Hecho cuando**: `from finances_downloader.ft_market_data_binance import cli` importa sin errores.
  - **Hecho**: `test_foundation.py::test_cli_module_imports`.
- [x] **T0.3**: Añadir modelos Pydantic a `packages/finances-core/src/finances_core/schemas/market_data.py` (DownloadRequest, DownloadPlan, DownloadPlanItem, DownloadProgress, DownloadResult, OHLCV_SCHEMA, OHLCV_PARTITION_COLS)
  - **Hecho cuando**: Modelos validan ejemplos JSON; MyPy strict pasa sin errors.
  - **Hecho**: `test_foundation.py::test_spec_plan_json_example_validates`, `test_models_reexport_core_contracts`; `mypy --strict finances_core/schemas/market_data.py` → Success.
- [x] **T0.4**: Extender `FinancesSettings` en `packages/finances-core/src/finances_core/config.py` con 12 nuevas variables `FT_BINANCE_*` y `FT_DOWNLOAD_MAX_CONCURRENT`
  - **Hecho cuando**: `FinancesSettings()` instancia con defaults; `model_config.env_prefix = "FT_"`.
  - **Hecho**: `test_foundation.py::test_settings_binance_defaults` (defaults + `env_prefix == "FT_"`).
- [x] **T0.5**: Actualizar `packages/finances-core/src/ft_setup/templates/env_list.conf.j2` con las 12 nuevas variables (metadatos: source_tool="ft_market_data_binance", since_version="0.1.0")
  - **Hecho cuando**: `ft_setup doctor` muestra las nuevas vars; `ft_setup init` regenera `.env.template` con ellas.
  - **Hecho**: `test_doctor_lists_tool_variables_with_source_tool[ft_market_data_binance]` y `test_init_env_template_includes_all_tool_variables` (packages/finances-core/tests/integration/test_setup_cli.py).
## Fase 1: Cliente HTTP Dual + Token Bucket

- [x] **T1.1**: Implementar `TokenBucket` genérico en `packages/finances-downloader/src/ft_market_data_binance/rate_limiter.py`
  - **Hecho cuando**: Test unitario verifica: capacidad, refill rate, `acquire()` bloquea cuando vacío, tokens se recargan por tiempo.
  - **Hecho**: `test_rate_limiter.py` (capacidad, bloqueo con bucket vacío, recarga, buckets aislados).
- [x] **T1.2**: Implementar `BinancePublicDataClient` en `client.py` — base_url, rate_limiter generoso, `HEAD` para verificar existencia, `GET` streaming para descargar .zip
  - **Hecho cuando**: Test de integración (mock HTTP) verifica: construye URLs mensuales correctas, respeta rate limit, maneja 404/200, streaming download a archivo temporal.
  - **Hecho**: `test_client.py`: `test_build_monthly_url_spot_and_futures`, `test_check_exists_200_returns_prefixed_checksum`, `test_check_exists_404_returns_false`, `test_download_streams_to_temp_file_and_validates_checksum`, `test_download_404_raises_not_found`, `test_public_download_retries_on_5xx`, `test_public_requests_respect_rate_limiter` (httpx.MockTransport, sin red).
- [x] **T1.3**: Implementar `BinanceRestClient` en `client.py` — base_url, rate_limiter estricto, `GET /klines` con paginación, retry con backoff exponencial + jitter (tenacity)
  - **Hecho cuando**: Test de integración (mock HTTP) verifica: paginación correcta, headers rate limit, retry en 429/5xx, backoff exponencial, agota retries → excepción.
  - **Hecho**: `test_client.py`: `test_fetch_klines_paginates`, `test_rest_retries_on_rate_limit_and_5xx[429|418|500|503]` (log `binance_http_retry`), `test_rest_retries_on_network_error`, `test_rest_does_not_retry_on_4xx`, `test_rest_exhausts_max_retries_then_raises` (FT_BINANCE_MAX_RETRIES), `test_backoff_is_exponential_from_settings` (FT_BINANCE_BASE_BACKOFF_MS).
- [x] **T1.4**: Implementar `checksum_sha256(file_path)` y validación en `client.py`
  - **Hecho cuando**: Test verifica: checksum coincide con contenido conocido; mismatch lanza excepción.
  - **Hecho**: `test_client.py`: `test_checksum_sha256_known_content`, `test_parse_checksum_file_binance_format` (formato `<hex>  <archivo>`), `test_checksums_match_ignores_prefix_and_case`, `test_download_checksum_mismatch_raises_and_cleans_temp`.

## Fase 2: Planificador (Pasos 1-6 del Pipeline)

- [x] **T2.1**: Implementar `parse_time_input(s: str) -> int` en `planner.py`
  - **Hecho cuando**: Tests: "2024-01-01" → 1704067200000, "1704067200000" → 1704067200000, "now" → timestamp actual ms, ISO con TZ → UTC ms.
  - **Hecho**: Implementado en `finances_core.timeutils` (planner ya no lo define); `test_planner.py::test_parse_time_input_comes_from_finances_core`.
- [x] **T2.2**: Implementar `inventory_local_parquet(parquet_dir, symbol, tf, market) -> List[Tuple[int,int]]` en `inventory.py`
  - **Hecho cuando**: Test con directorio Parquet temporal particionado: retorna rangos correctos; usa `pl.scan_parquet(...).select(pl.col("open_time_ms").min(), pl.col("open_time_ms").max()).collect()` sin leer datos completos.
  - **Hecho**: `test_inventory.py`: `test_inventory_reads_writer_layout` (layout Hive RF-4), `test_inventory_isolates_market_symbol_and_timeframe`, `test_inventory_ignores_temp_and_corrupt_files`, `test_inventory_uses_aggregation_without_loading_all_columns`.
- [x] **T2.3**: Implementar `compute_gaps(req_start, req_end, local_ranges) -> List[Tuple[int,int]]` en `inventory.py`
  - **Hecho cuando**: Tests: sin locales → gap completo; locales cubren todo → lista vacía; locales parciales → gaps correctos; ranges adyacentes/overlap → merge correcto.
  - **Hecho**: `test_inventory.py::test_gaps_*` (sin locales, cobertura total, parcial, adyacentes/solapados, fuera de rango).
- [x] **T2.4**: Implementar `check_public_data_availability(gaps, symbol, tf, market, client) -> Tuple[List[PlanItem], List[Tuple[int,int]]]` en `public_data.py`
  - **Hecho cuando**: Test (mock client): para cada gap, genera URLs mensuales correctas; HEAD 200 → PlanItem public_data; HEAD 404 → gap residual; respeta rate limit.
  - **Hecho**: `test_public_data.py`: `test_existing_months_become_public_items`, `test_each_missing_month_goes_to_rest` (cada mes ausente → REST), `test_contiguous_missing_months_are_merged`, `test_availability_respects_rate_limit`.
- [x] **T2.5**: Implementar `plan_rest_fallback(uncovered_gaps, symbol, tf, market, limit, client) -> List[PlanItem]` en `rest_api.py`
  - **Hecho cuando**: Test: gap de 5000 velas con limit=1000 → 5 PlanItems REST con startTime/endTime correctos; gap < limit → 1 PlanItem.
  - **Hecho**: `test_rest_api.py::test_5000_candles_with_limit_1000_gives_5_items`, `test_small_gap_gives_single_item`.
- [x] **T2.6**: Implementar `generate_download_plan(request: DownloadRequest) -> DownloadPlan` en `planner.py` (orquesta pasos 1-6)
  - **Hecho cuando**: Test de integración end-to-end (mock clients + FS temporal): request válida → plan con items public_data + rest_api, task_id UUID v7, covers_range_ms correctos, archivo JSON escrito en temp.
  - **Hecho**: `test_planner.py::test_generate_plan_end_to_end` (UUID v7, public + REST, covers_range_ms, JSON en temp), `test_plan_is_empty_when_local_data_covers_request`, `test_month_boundary_between_local_partitions_is_not_a_gap`, `test_unknown_symbol_is_skipped_with_warning`, `test_future_start_is_rejected`.

## Fase 3: Normalización y Escritura Parquet

- [x] **T3.1**: Implementar `normalize_binance_klines(df: pl.DataFrame, source: Literal["public_data","rest_api"]) -> pl.DataFrame` en `normalizer.py`
  - **Hecho cuando**: Tests:
    - Public Data CSV: 12 columnas → esquema canónico 13 columnas (añade symbol, tf, market, year, month); dtypes correctos; dedup por open_time_ms; ordenado.
    - REST JSON: array de 12 elementos → mismo resultado.
    - Validación: filas fuera de rango filtradas; schema Arrow coincide con OHLCV_SCHEMA.
  - **Hecho**: `test_normalizer.py` (REST y CSV → esquema canónico; CSV spot sin cabecera conserva la 1ª fila; CSV futures con cabecera; timestamps en µs → ms; filtrado de rango; dedup/orden; dtypes == OHLCV_SCHEMA).
- [x] **T3.2**: Implementar `write_parquet_partitioned(df: pl.DataFrame, parquet_dir: Path) -> List[str]` en `writer.py`
  - **Hecho cuando**: Test: escribe en estructura Hive correcta; `partition_by` funciona; compresión zstd; statistics=True; retorna lista de rutas; re-ejecutar sobrescribe partición (idempotencia).
  - **Hecho**: `test_writer.py`: `test_writes_full_hive_layout`, `test_file_schema_float32_int64_zstd_statistics`, `test_merge_keeps_existing_rows_outside_new_range` (RF-4: merge sin pérdida en lugar de sobrescritura), `test_atomic_replace_leaves_no_temp_files`, `test_failed_write_keeps_previous_partition`. Particionado manual por `group_by` (no `partition_by`).
- [x] **T3.3**: Test de idempotencia: misma descarga 2 veces → mismo número de archivos, mismo row count, mismos hashes
  - **Hecho cuando**: Test integra normalizer + writer + inventory; 2da ejecución no duplica rows.
  - **Hecho**: `test_writer.py::test_idempotent_double_run_same_files_rows_and_hashes`; end-to-end `test_service.py::test_rerun_is_idempotent_end_to_end`.

## Fase 4: Orquestador Principal (Paso 7)

- [x] **T4.1**: Implementar `execute_download_plan(plan: DownloadPlan, settings, semaphore) -> DownloadResult` en `service.py`
  - **Hecho cuando**: Test (mock clients): procesa items en paralelo con semáforo; actualiza estados en SQLTaskManager; agrupa DataFrames por (symbol, tf, market); normaliza; escribe Parquet; retorna DownloadResult con stats.
  - **Hecho**: `test_service.py::test_execute_plan_public_and_rest`, `test_semaphore_limits_concurrent_downloads` (pico == max_concurrent).
- [x] **T4.2**: Integrar `SQLTaskManager` para persistir progreso por fase (PLANNING, DOWNLOADING_PUBLIC, DOWNLOADING_REST, NORMALIZING, WRITING_PARQUET, COMPLETED/FAILED)
  - **Hecho cuando**: Test: cada transición de estado persiste en `share/tasks.db`; campos `downloaded_bytes`, `processed_candles`, `gaps_*` actualizados; `errors_json` captura errores por item.
  - **Hecho**: `test_service.py::test_phases_are_persisted_in_monotonic_order` (secuencia completa + contadores + errors_json), `test_progress_has_dto_shape`, `test_planning_failure_marks_task_failed`.
- [x] **T4.3**: Manejo de errores por item: fallo en un item no detiene los demás; se registra en `errors` y continúa
  - **Hecho cuando**: Test: 1 item falla (404 Public Data + REST agotado), otros 3 succeed → result.status=COMPLETED, errors contiene 1 entrada, gaps_unfillable poblado.
  - **Hecho**: `test_service.py::test_failed_item_does_not_stop_others` (404 Public Data + REST sin datos → COMPLETED, 1 error, gaps_unfillable), `test_network_exhausted_marks_failed_but_keeps_written_data`, `test_checksum_mismatch_retries_once_then_falls_back_to_rest`.

## Fase 5: CLI (Typer)

- [x] **T5.1**: Implementar `cli.py` con comandos `download`, `plan`, `status` + callback logging
  - **Hecho cuando**: `uv run finances-downloader binance --help` muestra 3 subcomandos; help descriptivo.
  - **Hecho**: `test_cli.py::test_import_and_help_lists_subcommands` (entrypoint real `ft_market_data_binance`; ahora 4 subcomandos: download, plan, status, result).
- [x] **T5.2**: `download`: parse args → DownloadRequest → genera plan → si `--dry-run` imprime plan y sale; sino ejecuta `execute_download_plan` con progress bars Rich
  - **Hecho cuando**: Test CLI (temp dir, mock clients): `--dry-run` imprime JSON plan; sin dry-run muestra progress bars por fase; tabla final con stats por símbolo.
  - **Hecho**: `test_cli.py::test_download_dry_run_prints_plan_json`, `test_download_executes_with_phases_and_table`.
- [x] **T5.3**: `plan <task_id>`: lee `download_plan_{task_id}.json` y muestra (json/yaml)
  - **Hecho cuando**: Test: archivo existe → imprime; no existe → error claro.
  - **Hecho**: `test_cli.py::test_plan_command` (sólo JSON; YAML eliminado, no figura en el spec).
- [x] **T5.4**: `status <task_id>`: consulta SQLTaskManager y muestra progreso
  - **Hecho cuando**: Test: task existe → muestra fase, contadores; no existe → error.
  - **Hecho**: `test_cli.py::test_status_command_outputs_progress_dto` (mismo DTO que la API), `test_result_command`.
- [x] **T5.5**: Flags globales: `--verbose/-v`, `--quiet/-q`, `--log-format [rich|json]`, `--max-concurrent` (sobrescribe env)
  - **Hecho cuando**: Tests verifican cada flag afecta comportamiento; `--max-concurrent` pasa a `DownloadRequest.max_concurrent`.
  - **Hecho**: `test_cli.py::test_global_flags_configure_logging[*]`, `test_invalid_log_format_is_usage_error`, `test_download_level_flags_override`, `test_json_log_format_emits_json_logs`, `test_max_concurrent_flag_reaches_request`.
- [x] **T5.6**: Exit codes: `0` OK, `1` error genérico, `2` validación entrada, `3` error descarga (parcial), `4` IO/permiso
  - **Hecho cuando**: Tests `pytest -k cli_exit_codes` pasan.
  - **Hecho**: `pytest -k cli_exit_codes` → `test_cli_exit_codes_{ok,validation,future_message,download_failed,io_error,generic_error}`.

## Fase 6: API (FastAPI)

- [x] **T6.1**: Implementar `api.py` con router `/market-data/binance` y 4 endpoints
  - **Hecho cuando**: `ft_serve` (paquete `finances-api`) levanta; OpenAPI docs en `/docs` muestran endpoints.
  - **Hecho**: `finances_api.create_app()` monta el router; `test_openapi_includes_all_tool_routers` y los tests de `ft_serve` (packages/finances-api/tests/test_app.py) verifican arranque y rutas en OpenAPI.
- [x] **T6.2**: `POST /download` → valida → genera plan → `BackgroundTasks.add_task(execute_download_plan)` → retorna `202` con `task_id`, `poll_url`
  - **Hecho cuando**: Test: response schema correcto; task_id UUID v7; poll_url absoluta; tarea se ejecuta en background.
  - **Hecho**: `test_api.py::test_post_dispatches_immediately_with_absolute_poll_url` (202, UUID v7, URL absoluta, tarea PENDING antes del background, sin red en el endpoint), `test_post_invalid_request_is_400`.
- [x] **T6.3**: `GET /status/{task_id}` → retorna `DownloadProgress` actual
  - **Hecho cuando**: Test polling: POST → GET status (PENDING→PLANNING→...) → COMPLETED.
  - **Hecho**: `test_api.py::test_polling_reaches_completed` (PLANNING→…→COMPLETED), `test_status_unknown_task_404`, `test_get_endpoints_offload_blocking_io`.
- [x] **T6.4**: `GET /plan/{task_id}` → retorna `DownloadPlan` (archivo JSON)
  - **Hecho cuando**: Test: plan existe → 200 + JSON; no existe → 404.
  - **Hecho**: `test_api.py::test_plan_endpoint`.
- [x] **T6.5**: `GET /result/{task_id}` → retorna `DownloadResult` final
  - **Hecho cuando**: Test: task COMPLETED → 200 + parquet_paths + stats; task FAILED → 200 + errors.
  - **Hecho**: `test_api.py::test_result_completed_with_paths_stats_and_unfillable`, `test_result_failed_returns_200_with_errors` (errores en `stats["errors"]`), `test_result_unfillable_gap_is_reported`, `test_result_pending_409_and_unknown_404`.

## Fase 7: Tests y Calidad (Art. 7, 17 Constitución)

- [x] **T7.1**: Tests unitarios: `rate_limiter.py`, `inventory.py`, `normalizer.py`, `planner.py` (funciones puras)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/unit/ -v` pasa; cobertura ≥ 95%.
  - **Hecho**: Tests unitarios en `packages/finances-downloader/tests/ft_market_data_binance/` (no en `tests/unit/`: layout por herramienta). Cobertura del paquete de la herramienta 97% (rate_limiter 100%, inventory 95%, normalizer 100%, planner 98%).
- [x] **T7.2**: Tests de integración: `client.py` (mock HTTP), `public_data.py`, `rest_api.py`, `writer.py` (FS real temp)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/ -v` pasa.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_binance/test_client.py`, `test_public_data.py`, `test_rest_api.py`, `test_writer.py` (mock HTTP + FS temporal).
- [x] **T7.3**: Tests de integración CLI: todos los comandos con temp dir, mock clients
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/test_cli.py -v` pasa.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_binance/test_cli.py`.
- [x] **T7.4**: Tests de integración API: todos los endpoints con `httpx.AsyncClient` (testserver)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/test_api.py -v` pasa.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_binance/test_api.py` (TestClient sobre app que monta `router`).
- [x] **T7.5**: Tests de contrato: CLI vs API producen mismo resultado para mismos inputs
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/contract/ -v` pasa.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_binance/test_contract.py` (status/result/plan CLI == API; misma descarga → mismas stats).
- [x] **T7.6**: Configurar `ruff` + `mypy --strict` en `finances-downloader`; `pre-commit` hooks
  - **Hecho cuando**: `uv run ruff check packages/finances-downloader` && `uv run mypy --strict packages/finances-downloader` → 0 errors.
  - **Hecho**: `ruff check packages/finances-downloader` → All checks passed; `mypy --strict` del paquete completo → Success (24 archivos); hooks pre-commit locales `mypy-finances-*` + ruff/ruff-format en `.pre-commit-config.yaml`.
## Fase 8: Documentación y Cierre

- [x] **T8.1**: Actualizar `README.md` sección "Inicio Rápido" con ejemplos `ft_market_data_binance download --symbol BTCUSDT --timeframe 1h --start 2024-01-01`
  - **Hecho cuando**: README refleja comandos reales con ejemplos copy-pasteables.
  - **Hecho**: README §3 incluye `download`, `status` y `result` con ejemplos copy-pasteables.
- [ ] **T8.2**: Verificar matriz Art. 20 Constitución (100% dimensiones) via checklist interno
  - **Hecho cuando**: Checklist firmado en PR description; CI pipeline verde.
  - **Pendiente**: Matriz Art. 20 pendiente de firma.
- [x] **T8.3**: Registrar router en `finances-api` (`ft_serve`)
  - **Hecho cuando**: `finances_api.create_app()` importa e incluye `binance_router`; health-check incluye parquet dir accesible.
  - **Hecho**: `finances_api.create_app()` incluye `binance_router`; `GET /health` reporta `parquet_dir_accessible` (`test_health_reports_accessible_parquet_dir`).
- [x] **T8.4**: Validar que `ft_setup doctor` detecta y lista las 12 nuevas variables `FT_BINANCE_*` y `FT_DOWNLOAD_MAX_CONCURRENT`
  - **Hecho cuando**: `ft_setup doctor` output incluye todas con `source_tool=ft_market_data_binance`.
  - **Hecho**: `test_doctor_lists_tool_variables_with_source_tool[ft_market_data_binance]` y `test_init_env_template_includes_all_tool_variables` (packages/finances-core/tests/integration/test_setup_cli.py).