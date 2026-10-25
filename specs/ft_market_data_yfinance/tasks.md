# Tasks — `ft_market_data_yfinance` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` (ver CI).

## Fase 1: Núcleo compartido `finances_core.marketdata` (Art. 4)

- [x] **T1.1**: `marketdata/hive_writer.py` (extraído de `ft_market_data_binance/writer.py`, parametrizado por `exchange` y esquema de datos)
  - **Hecho cuando**: tests del núcleo cubren layout Hive por `exchange`, merge sin pérdida (re-ejecución con rango menor no borra filas), dedupe prevaleciendo lo nuevo y escritura atómica.
  - **Hecho**: `packages/finances-core/tests/unit/test_marketdata_hive_writer.py` (8 tests: `test_partition_dir_is_parametrized_by_exchange`, `test_write_uses_exchange_layout_and_schema_columns`, `test_narrower_rerun_keeps_existing_rows`, `test_dedupe_keeps_newest_row`, `test_duplicates_inside_batch_keep_last`, `test_failed_atomic_write_keeps_previous_file`, ...) en verde.
- [x] **T1.2**: `marketdata/inventory.py` (`inventory_local_parquet` con `exchange` y `market_type="*"`, `compute_gaps`)
  - **Hecho cuando**: test encuentra particiones bajo cualquier `market_type` y calcula gaps con cobertura hasta última vela + 1 timeframe.
  - **Hecho**: `test_marketdata_inventory.py` (`test_inventory_any_market_type_finds_all`, `test_inventory_keeps_symbol_verbatim`, `test_gaps_with_coverage_until_last_candle_plus_timeframe`, ...; `coverage_ranges` añadido al núcleo) en verde.
- [x] **T1.3**: `marketdata/plans.py`, `tasks_lifecycle.py`, `retry.py`, `rate_limit.py`, `months.py`
  - **Hecho cuando**: tests del núcleo cubren persistencia de planes en `share/plans/`, fases monótonas, retry solo ante errores reintentables, TokenBucket.
  - **Hecho**: `test_marketdata_plans.py` (`test_save_and_load_roundtrip` en `share/plans/`), `test_marketdata_tasks_lifecycle.py` (`test_phases_only_advance`, `test_mark_failed_records_error`), `test_marketdata_retry.py` (`test_retries_only_retryable_errors_then_succeeds`, `test_non_retryable_error_is_not_retried`), `test_marketdata_rate_limit_months.py` (TokenBucket + meses) en verde. `retry.py` usa `RetryPolicy(max_retries, base_backoff_ms, jitter)`; dependencia `tenacity>=8.2` añadida a finances-core.
- [x] **T1.4**: `ft_market_data_binance` usa `finances_core.marketdata` (borra sus copias)
  - **Hecho cuando**: los 134 tests de binance pasan **sin cambios en sus aserciones**; ruff + mypy strict limpios.
  - **Hecho**: `pytest -q tests/ft_market_data_binance` → `134 passed` sin tocar `tests/` (git diff vacío); finances-downloader: ruff check `All checks passed!`, ruff format `50 files already formatted`, mypy `Success: no issues found in 24 source files`, pytest `313 passed`; finances-api: mypy OK, `8 passed`.
- [x] **T1.5**: Schemas y settings: `schemas/market_data_yfinance.py`, `FT_YFINANCE_*` en `config.py`, se quita `YFINANCE_API_KEY`
  - **Hecho cuando**: tests validan defaults y validaciones de `YFDownloadRequest` (timeframe no canónico rechazado, `symbols` 1..100).
  - **Hecho**: `test_schemas_market_data_yfinance.py` (`test_request_rejects_non_canonical_timeframe`, `test_request_symbols_between_1_and_100`, `test_yfinance_settings_defaults`, ...) en verde; `DispatchResponse` movido a `finances_core/schemas/dispatch.py`; `env_list.conf(.j2)` actualizados (idénticos) y `test_doctor_lists_tool_variables_with_source_tool[ft_market_data_yfinance]` pasa (adelanta T5.3). finances-core: `149 passed`, ruff/format/mypy limpios.

## Fase 2: Adaptador y planificación

- [x] **T2.1**: `timeframes.py` — mapa canónico↔Yahoo, `LOOKBACK_DAYS`, `MAX_WINDOW_DAYS`, `split_by_limits`
  - **Hecho cuando**: tests: 1m de 20 días se parte en ventanas ≤ 8 días; 5m de hace 90 días → parte `beyond_yahoo_lookback`; 1d de 30 años = un pedido.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_timeframes.py` (`test_1m_of_20_days_is_split_in_windows_of_at_most_8_days`, `test_5m_from_90_days_ago_is_partly_beyond_lookback`, `test_1d_of_30_years_is_a_single_request`, tabla RF-3 y duraciones mín./máx. de vela) en verde.
- [x] **T2.2**: `adapter.py` — `fetch_history`, `YahooBatch`, clasificación de errores, `default_fetcher()` inyectable
  - **Hecho cuando**: tests con un `Ticker` simulado verifican `auto_adjust=False`, `actions=True`, conversión pandas→Polars, `instrumentType`, y que 429 → reintentable, "No data found" → `SymbolNotFoundError`.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_adapter.py` con `FakeTicker` (`conftest.py`, índice tz-aware y excepciones reales de yfinance 1.7): `test_history_called_unadjusted_with_actions_and_timeout`, `test_pandas_frame_is_converted_to_polars_with_utc_ms`, `test_instrument_type_comes_from_history_metadata`, `test_rate_limit_429_is_retryable`, `test_no_data_found_is_symbol_not_found_and_not_retryable` (+ `YFTzMissingError`, red/timeout, ventana vacía) en verde.
- [x] **T2.3**: `planner.py` — plan puro (sin red) + persistencia
  - **Hecho cuando**: test con datos locales previos solo planifica lo posterior a la última vela; plan guardado y recuperable con `load_plan`.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_planner.py` (`test_existing_data_only_plans_after_last_candle`, `test_plan_is_saved_and_loadable`, `test_inventory_considers_any_market_type`, `test_beyond_lookback_goes_to_gaps_unfillable`, `test_planner_does_not_touch_the_network` vía AST) en verde.

## Fase 3: Normalización y persistencia

- [x] **T3.1**: `normalizer.py` — RF-4 (UTC ms, float32, `adj_close`, `market_type` mapeado, `other` para desconocidos)
  - **Hecho cuando**: tests con lotes de AAPL (NY), EURUSD=X (Londres) y BTC-USD (UTC) producen `open_time_ms` correctos y dtypes de `YF_OHLCV_SCHEMA`.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_normalizer.py` (`test_aapl_daily_new_york_midnight_in_utc` incl. cambio EST→EDT, `test_eurusd_hourly_london`, `test_btc_usd_daily_utc`, `test_dtypes_and_columns_follow_schema`, `test_unknown_instrument_type_is_other_with_warning`, `test_forming_candle_is_dropped`) en verde.
- [x] **T3.2**: Escritura de velas con `hive_writer` (`exchange="yfinance"`)
  - **Hecho cuando**: test escribe `yfinance/market_type=equity/symbol=AAPL/timeframe=1d/year=2024/month=01/data.parquet` y una re-ejecución parcial no pierde filas.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_persistence.py` (`test_candles_are_written_in_yfinance_hive_layout`, `test_partial_rerun_does_not_lose_rows`, `test_symbols_with_special_characters_are_written_verbatim`) en verde.
- [x] **T3.3**: `events.py` — tabla de eventos sin pérdida
  - **Hecho cuando**: test con dividendo + split 4:1 (AAPL ago-2020) y capital gain (ETF) escribe `yfinance_events/symbol=AAPL/events.parquet`; re-ejecución no duplica; lote sin eventos no crea archivo.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_persistence.py` (`test_dividend_and_split_written_to_events_table`, `test_capital_gain_for_etf`, `test_rerun_does_not_duplicate_and_merges_losslessly`, `test_batch_without_events_does_not_create_file`, `..._does_not_modify_existing_file`) en verde.

## Fase 4: Servicio, CLI y API

- [x] **T4.1**: `service.py` — orquestador con fases RF-8, concurrencia, TokenBucket y reintentos RF-7
  - **Hecho cuando**: tests: tarea COMPLETED con stats por símbolo/timeframe; 429 persistente → FAILED con datos ya escritos intactos; símbolo inexistente → COMPLETED con `errors`.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_service.py` (`test_completed_with_stats_per_symbol_and_timeframe`, `test_persistent_429_fails_and_keeps_written_data`, `test_unknown_symbol_completes_with_errors`, `test_rerun_is_lossless_and_only_requests_after_last_candle`, `test_market_closures_are_not_gaps_nor_errors`, `test_beyond_lookback_is_unfillable_without_request`, `test_concurrency_is_bounded`, `test_429_retries_then_succeeds`) en verde.
- [x] **T4.2**: `cli.py` — `download` (`--dry-run`), `plan`, `status`, `result`; flags globales; exit codes RF-9
  - **Hecho cuando**: tests CliRunner por comando y por exit code (0/1/2/3/4).
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_cli.py` (`download`/`--dry-run`/`plan`/`status`/`result`, flags `-v/-q/--log-format`, `test_exit_0_ok`, `test_exit_1_generic_error`, `test_unknown_task_is_exit_1`, `test_exit_2_validation`, `test_exit_3_task_failed`, `test_exit_3_with_real_persistent_429`, `test_exit_4_io_error`) en verde; script `ft_market_data_yfinance` instalado.
- [x] **T4.3**: `api.py` — `POST /download` 202 inmediato, `GET /status|/plan|/result` (404/409), trabajo pesado fuera del event loop
  - **Hecho cuando**: tests con `TestClient`; test de contrato: JSON de CLI == JSON de API para `status`/`plan`/`result`.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_api.py` (202 inmediato con tarea PENDING, 400, 404, 409, 422, `run_in_threadpool` en los GET) y `packages/finances-downloader/tests/ft_market_data_yfinance/test_contract.py::test_cli_and_api_expose_same_status_plan_and_result` en verde.
- [x] **T4.4**: Aislamiento Art. 4
  - **Hecho cuando**: test AST verifica que la herramienta no importa ningún otro `ft_*`, y que solo `adapter.py` importa `yfinance`/`pandas`.
  - **Hecho**: `packages/finances-downloader/tests/ft_market_data_yfinance/test_foundation.py` (`test_no_cross_imports_to_sibling_tools`, `test_only_adapter_imports_yfinance_or_pandas`) en verde. finances-downloader: ruff `All checks passed!`, format `71 files already formatted`, mypy `Success: no issues found in 33 source files`, pytest `439 passed`; finances-core `149 passed`, finances-api `8 passed`.

## Fase 5: Integración con la suite

- [x] **T5.1**: `ft_data_viewer` lista y resume datasets `yfinance` (`DatasetIdentifier.market_type` ampliado; `yfinance_events/` ignorado)
  - **Hecho cuando**: tests del viewer: `list` muestra `yfinance/equity/AAPL/1d`; `summary` funciona; el árbol de eventos no aparece como dataset.
  - **Hecho**: `DatasetIdentifier.market_type` admite los 10 valores; ayuda de CLI/API generada de `MARKET_TYPES` (`scanner.MARKET_HELP`). `packages/finances-downloader/tests/ft_data_viewer/test_yfinance_datasets.py` (RED por `literal_error` → GREEN): `test_list_shows_yfinance_dataset_and_ignores_events_tree` (sin warnings con `yfinance_events/symbol=AAPL/events.parquet`), `test_summary_works_on_yfinance_dataset` (incl. `adj_close`), `test_symbols_with_special_characters_resolve` (`^GSPC`, `EURUSD=X`, `BTC-USD`), `test_cli_list_and_summary_on_yfinance`, `test_list_filters_by_yahoo_market_type`, `test_market_types_include_yahoo_values`, `test_cli_help_lists_widened_market_types`; `tests/ft_data_viewer` → `187 passed`.
- [x] **T5.2**: `finances_api` monta `/market-data/yfinance`
  - **Hecho cuando**: test de OpenAPI incluye las 4 rutas.
  - **Hecho**: `create_app()` incluye `yfinance_router`; `packages/finances-api/tests/test_app.py`: `test_openapi_includes_all_tool_routers` (prefijo `/market-data/yfinance/`, RED → GREEN) y `test_openapi_includes_the_four_yfinance_routes` (POST `/download`, GET `/status|/plan|/result/{task_id}`); finances-api `9 passed`.
- [x] **T5.3**: `ft_setup`: `FT_YFINANCE_*` en `env_list.conf(.j2)`, sin `FT_YFINANCE_API_KEY`
  - **Hecho cuando**: `test_doctor_lists_tool_variables_with_source_tool[ft_market_data_yfinance]` pasa.
  - **Hecho**: `env_list.conf` y `templates/env_list.conf.j2` idénticos (`cmp`) con los 5 `FT_YFINANCE_*`; `test_doctor_lists_tool_variables_with_source_tool[ft_market_data_yfinance-expected2]` PASSED (aserción `FT_YFINANCE_API_KEY not in names`); finances-core `149 passed`.
- [ ] **T5.4**: `pyproject.toml` (dep `yfinance`, script, mypy override) y README (sin *planificado*, ejemplos copy-pasteables)
  - **Hecho cuando**: CI instala desde cero y pasa en Windows y Ubuntu.
  - **Avance**: `pyproject.toml` listo (dep `yfinance>=1.7`, script `ft_market_data_yfinance`, override mypy `yfinance.*`, `pandas-stubs` en dev).
  - **Avance**: README sin *(planificado)* para la herramienta, §2.2 descrita (OHLC ajustado por splits, no por dividendos + `adj_close` + eventos), ejemplos de §3 (`download` AAPL 1d, `"^GSPC"` 1d, `"EURUSD=X"` 1h, `--dry-run`, `status|plan|result`) verificados contra `--help` y en el smoke T6.1, rutas `/market-data/yfinance` en la sección de servicio. CI (`.github/workflows/ci.yml`) ya instala `finances-downloader[dev]`; `pip install --dry-run -e "packages/finances-downloader[dev]"` resuelve (`Would install finances-downloader-0.1.0`, yfinance/pandas satisfechos). **Pendiente**: la ejecución real de CI (Windows + Ubuntu) ocurre al hacer push.

## Fase 6: Cierre

- [x] **T6.1**: Smoke real contra Yahoo (fuera de CI, `FT_PROJECT_ROOT` temporal): AAPL 1d, ^GSPC 1d, EURUSD=X 1h, BTC-USD 5m; re-ejecución sin pérdida; `ft_data_viewer list` los muestra; `ft_serve` expone las rutas
  - **Hecho cuando**: salida del smoke registrada en este archivo.
  - **Hecho** (2026-10-25, yfinance 1.7, `FT_PROJECT_ROOT` temporal tras `ft_setup init`):
    - Run 1 (exit 0, COMPLETED, 0 errores): `AAPL/1d` equity 706 velas · `^GSPC/1d` index 706 · `EURUSD=X/1h` fx 927 (desde 2026-09-01) · `BTC-USD/5m` crypto 1670 (desde 2026-10-20); 68 Parquet diarios + `yfinance_events/symbol=AAPL/events.parquet`.
    - Run 2 (mismos comandos, exit 0, COMPLETED): `processed_candles=0`, filas por dataset sin cambios (706/706/927/1670, total 4009 → 4009). AAPL+^GSPC: 38 pedidos vacíos (`gaps_found=38`) = límites de mes en fin de semana/feriado + cola posterior a la última vela (limitación conocida, RF-6); EURUSD=X y BTC-USD: 1 pedido de cola cada uno.
    - `ft_data_viewer list`: 4 datasets `yfinance` (crypto BTC-USD 5m, equity AAPL 1d, fx EURUSD=X 1h, index ^GSPC 1d), sin warnings en stderr con `yfinance_events/` presente; `summary -m index -s ^GSPC` → 706 filas con `adj_close`.
    - App compuesta en proceso (`httpx.ASGITransport` sobre `finances_api.app.create_app()` con lifespan): `/openapi.json` expone `/market-data/yfinance/{download, status/{task_id}, plan/{task_id}, result/{task_id}}`; `GET /data/datasets?exchange=yfinance` → 200, 4 datasets; `/health` ok.
- [ ] **T6.2**: Verificar matriz Art. 20 vía checklist del PR
  - **Hecho cuando**: checklist firmado en la descripción del PR; CI verde.
