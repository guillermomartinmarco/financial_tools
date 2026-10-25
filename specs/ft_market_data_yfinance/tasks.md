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

- [ ] **T2.1**: `timeframes.py` — mapa canónico↔Yahoo, `LOOKBACK_DAYS`, `MAX_WINDOW_DAYS`, `split_by_limits`
  - **Hecho cuando**: tests: 1m de 20 días se parte en ventanas ≤ 8 días; 5m de hace 90 días → parte `beyond_yahoo_lookback`; 1d de 30 años = un pedido.
- [ ] **T2.2**: `adapter.py` — `fetch_history`, `YahooBatch`, clasificación de errores, `default_fetcher()` inyectable
  - **Hecho cuando**: tests con un `Ticker` simulado verifican `auto_adjust=False`, `actions=True`, conversión pandas→Polars, `instrumentType`, y que 429 → reintentable, "No data found" → `SymbolNotFoundError`.
- [ ] **T2.3**: `planner.py` — plan puro (sin red) + persistencia
  - **Hecho cuando**: test con datos locales previos solo planifica lo posterior a la última vela; plan guardado y recuperable con `load_plan`.

## Fase 3: Normalización y persistencia

- [ ] **T3.1**: `normalizer.py` — RF-4 (UTC ms, float32, `adj_close`, `market_type` mapeado, `other` para desconocidos)
  - **Hecho cuando**: tests con lotes de AAPL (NY), EURUSD=X (Londres) y BTC-USD (UTC) producen `open_time_ms` correctos y dtypes de `YF_OHLCV_SCHEMA`.
- [ ] **T3.2**: Escritura de velas con `hive_writer` (`exchange="yfinance"`)
  - **Hecho cuando**: test escribe `yfinance/market_type=equity/symbol=AAPL/timeframe=1d/year=2024/month=01/data.parquet` y una re-ejecución parcial no pierde filas.
- [ ] **T3.3**: `events.py` — tabla de eventos sin pérdida
  - **Hecho cuando**: test con dividendo + split 4:1 (AAPL ago-2020) y capital gain (ETF) escribe `yfinance_events/symbol=AAPL/events.parquet`; re-ejecución no duplica; lote sin eventos no crea archivo.

## Fase 4: Servicio, CLI y API

- [ ] **T4.1**: `service.py` — orquestador con fases RF-8, concurrencia, TokenBucket y reintentos RF-7
  - **Hecho cuando**: tests: tarea COMPLETED con stats por símbolo/timeframe; 429 persistente → FAILED con datos ya escritos intactos; símbolo inexistente → COMPLETED con `errors`.
- [ ] **T4.2**: `cli.py` — `download` (`--dry-run`), `plan`, `status`, `result`; flags globales; exit codes RF-9
  - **Hecho cuando**: tests CliRunner por comando y por exit code (0/1/2/3/4).
- [ ] **T4.3**: `api.py` — `POST /download` 202 inmediato, `GET /status|/plan|/result` (404/409), trabajo pesado fuera del event loop
  - **Hecho cuando**: tests con `TestClient`; test de contrato: JSON de CLI == JSON de API para `status`/`plan`/`result`.
- [ ] **T4.4**: Aislamiento Art. 4
  - **Hecho cuando**: test AST verifica que la herramienta no importa ningún otro `ft_*`, y que solo `adapter.py` importa `yfinance`/`pandas`.

## Fase 5: Integración con la suite

- [ ] **T5.1**: `ft_data_viewer` lista y resume datasets `yfinance` (`DatasetIdentifier.market_type` ampliado; `yfinance_events/` ignorado)
  - **Hecho cuando**: tests del viewer: `list` muestra `yfinance/equity/AAPL/1d`; `summary` funciona; el árbol de eventos no aparece como dataset.
- [ ] **T5.2**: `finances_api` monta `/market-data/yfinance`
  - **Hecho cuando**: test de OpenAPI incluye las 4 rutas.
- [ ] **T5.3**: `ft_setup`: `FT_YFINANCE_*` en `env_list.conf(.j2)`, sin `FT_YFINANCE_API_KEY`
  - **Hecho cuando**: `test_doctor_lists_tool_variables_with_source_tool[ft_market_data_yfinance]` pasa.
- [ ] **T5.4**: `pyproject.toml` (dep `yfinance`, script, mypy override) y README (sin *planificado*, ejemplos copy-pasteables)
  - **Hecho cuando**: CI instala desde cero y pasa en Windows y Ubuntu.

## Fase 6: Cierre

- [ ] **T6.1**: Smoke real contra Yahoo (fuera de CI, `FT_PROJECT_ROOT` temporal): AAPL 1d, ^GSPC 1d, EURUSD=X 1h, BTC-USD 5m; re-ejecución sin pérdida; `ft_data_viewer list` los muestra; `ft_serve` expone las rutas
  - **Hecho cuando**: salida del smoke registrada en este archivo.
- [ ] **T6.2**: Verificar matriz Art. 20 vía checklist del PR
  - **Hecho cuando**: checklist firmado en la descripción del PR; CI verde.
