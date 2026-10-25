# Tasks — `ft_data_viewer` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).

## Fase 0: Fundación y Modelos Compartidos

- [x] **T0.1**: Añadir modelos Pydantic a `packages/finances-core/src/finances_core/schemas/data_viewer.py` (DatasetIdentifier, DatasetInfo, DatasetListResponse, DatasetSummary, ColumnStats, GapInfo, AnomalyReport, ExportRequest, CompareRequest, CompareResult)
  - **Hecho cuando**: Modelos validan ejemplos JSON; MyPy strict pasa sin errors.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_contract.py::test_schemas_validate_json_examples` valida ejemplos JSON (y rechazo de market_type inválido); `mypy --strict finances_core/schemas/data_viewer.py` → Success.
- [x] **T0.2**: Extender `FinancesSettings` en `packages/finances-core/src/finances_core/config.py` con `DATA_VIEWER_SAMPLE_MAX_ROWS`, `DATA_VIEWER_GAP_THRESHOLD_MS`
  - **Hecho cuando**: `FinancesSettings()` instancia con defaults correctos.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_contract.py::test_settings_defaults` (10000 / 0).
- [x] **T0.3**: Actualizar `packages/finances-core/src/ft_setup/templates/env_list.conf.j2` con 2 nuevas variables (source_tool="ft_data_viewer", since_version="0.1.0")
  - **Hecho cuando**: `ft_setup doctor` muestra las nuevas vars; `ft_setup init` regenera `.env.template`.
  - **Hecho**: `test_doctor_lists_tool_variables_with_source_tool[ft_data_viewer]` y `test_init_env_template_includes_all_tool_variables` (packages/finances-core/tests/integration/test_setup_cli.py).
- [x] **T0.4**: Añadir entry point `ft_data_viewer` en `packages/finances-downloader/pyproject.toml` `[project.scripts]`
  - **Hecho cuando**: `pip install -e packages/finances-downloader` instala comando `ft_data_viewer`.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_contract.py::test_console_script_entry_point_installed` (entry point `finances_downloader.ft_data_viewer.cli:app` instalado en el venv).
- [x] **T0.5**: Crear estructura de carpetas `packages/finances-downloader/src/finances_downloader/ft_data_viewer/`
  - **Hecho cuando**: Directorio existe con `__init__.py`.
  - **Hecho**: Directorio con `__init__.py`; importado por toda la suite (177 tests).

## Fase 1: Scanner y Metadata (Lectura Lazy)

- [x] **T1.1**: Implementar `scan_parquet_partitions(parquet_dir: Path) -> List[DatasetInfo]` en `scanner.py`
  - **Hecho cuando**: Test con directorio Parquet temporal particionado (Hive) retorna lista correcta de datasets con year_range, month_count, estimated_rows, size_bytes.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_scanner.py` (layout Hive RF-4 completo `exchange/market_type=/symbol=/timeframe=/year=/month=`; varios exchanges/mercados/símbolos/TF; year_range, month_count, estimated_rows desde footers, size_bytes, filtro de market_type inválido, lista vacía, callback de progreso).
- [x] **T1.2**: Implementar `read_dataset_metadata(dataset_path: Path) -> Dict[str, ColumnStats]` en `scanner.py`
  - **Hecho cuando**: Test verifica stats por columna (min, max, null_count) leídas solo del footer Parquet, sin leer filas (mean/distinct_count se calculan en `summary`, ver RF-2).
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_scanner.py::test_read_dataset_metadata_*` — estadísticas de footer Parquet (min/max/null_count) con `pl.read_parquet`/`LazyFrame.collect` parcheados para fallar (no lee filas). Desviación: `pl.all().meta()` no existe en Polars 2.x y `mean`/`distinct_count` no están en las estadísticas Parquet → quedan `None` aquí y los completa `compute_summary` (plan.md §3 actualizado).
- [x] **T1.3**: Implementar `resolve_dataset_path(base_dir, identifier: DatasetIdentifier) -> Path` en `scanner.py`
  - **Hecho cuando**: Test resuelve path correcto para binance/spot/BTCUSDT/1h.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_scanner.py::test_resolve_dataset_path_*` (ruta exacta binance/spot/BTCUSDT/1h; `DatasetNotFoundError` para símbolo/mercado/exchange/TF inexistentes o carpeta sin Parquet).
- [x] **T1.4**: Tests unitarios `scanner.py` con dataset sintético (múltiples exchanges, symbols, timeframes)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_scanner.py -v` pasa; cobertura ≥ 95%.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_scanner.py` pasa; cobertura `scanner.py` 97%. (Ruta de tests: `tests/ft_data_viewer/` en lugar de `tests/unit/`.)

## Fase 2: Analyzer (Gaps, Anomalies, Stats)

- [x] **T2.1**: Implementar `compute_summary(lf: pl.LazyFrame, identifier: DatasetIdentifier) -> DatasetSummary` en `analyzer.py`
  - **Hecho cuando**: Test con LazyFrame conocido retorna row_count, date_range, ColumnStats por columna, gaps_detected=0, duplicate_timestamps=0, outlier_count=0.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_analyzer.py::test_compute_summary_*` (dataset limpio → 0/0/0; con metadata y sin metadata coinciden; conteo de gaps/duplicados/outliers; frame vacío).
- [x] **T2.2**: Implementar `detect_gaps(lf: pl.LazyFrame, timeframe_ms: int, min_gap_candles: int) -> List[GapInfo]` en `analyzer.py`
  - **Hecho cuando**: Test con gaps artificiales (eliminar filas) detecta correctamente gap_start_ms, gap_end_ms, missing_candles, gap_duration_human; respeta `min_gap_candles`.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_analyzer.py::test_detect_gaps_*` (gap_start/end, missing, duración humana, `min_gap_candles`, `gap_threshold_ms`, entrada desordenada con duplicados, validación de args).
- [x] **T2.3**: Implementar `detect_anomalies(lf: pl.LazyFrame) -> AnomalyReport` en `analyzer.py`
  - **Hecho cuando**: Test con dataset sintético conteniendo: 3 timestamps duplicados, 5 nulls en 'close', 2 outliers precio (zscore>3), 10 volumen=0 → reporte correcto en todas las categorías.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_analyzer.py::test_detect_anomalies_full_report` (3 duplicados, 5 nulls en close, 2 outliers z>3, 10 volumen=0) + conteo de duplicados no truncado, IQR, volumen alto, dataset limpio.
- [x] **T2.4**: Implementar `sample_rows(lf: pl.LazyFrame, n: int, mode: Literal["head","tail","sample"], cols: Optional[List[str]]) -> pl.DataFrame` en `analyzer.py`
  - **Hecho cuando**: `head/tail` usa `sort + limit`; `sample` usa reservoir sampling (Vitter R) sin cargar todo; proyección de columnas funciona.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_analyzer.py` (`head/tail` sort+limit con proyección sin `open_time_ms`; `sample` = reservoir sampling Algorithm L (familia Vitter R) que solo materializa `n` filas — verificado con spy de `collect`; uniformidad de `reservoir_indices`).
- [x] **T2.5**: Tests de integración `analyzer.py` con dataset Parquet real temporal
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_analyzer.py -v` pasa; cobertura ≥ 90%.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_analyzer.py` pasa (Parquet temporal real); cobertura `analyzer.py` 96%.

## Fase 3: Exporter y Comparator

- [x] **T3.1**: Implementar `stream_export(lf: pl.LazyFrame, format: Literal["parquet","csv","jsonl"], output: Union[Path, IO]) -> int` en `exporter.py`
  - **Hecho cuando**: Test exporta a Parquet/CSV/JSONL streaming (usa `sink_parquet/sink_csv/sink_ndjson`); retorna row count; no OOM en dataset grande simulado.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_exporter_comparator.py` (Parquet/CSV/JSONL a ruta y a stream binario, spy de `sink_parquet`, 500k filas simuladas, formato inválido).
- [x] **T3.2**: Implementar `compare_datasets(lf_a: pl.LazyFrame, lf_b: pl.LazyFrame, identifier_a, identifier_b) -> CompareResult` en `comparator.py`
  - **Hecho cuando**: Test con dos datasets idénticos salvo 5 filas con close divergente → price_divergence_max correcto, gaps_only_in_a/b vacíos, overlap correcto.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_exporter_comparator.py::test_compare_identical_except_five_closes` + `gaps_only_in_a/b` implementados (diferencia de intervalos recortada al solape), sin solape, volumen, lado vacío.
- [x] **T3.3**: Tests de integración `exporter.py` + `comparator.py`
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_exporter_comparator.py -v` pasa.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_exporter_comparator.py` pasa.

## Fase 4: Service Layer (Orquestador Compartido)

- [x] **T4.1**: Implementar `service.py` con funciones: `list_datasets()`, `get_summary()`, `get_rows()`, `get_gaps()`, `get_anomalies()`, `export_data()`, `compare_datasets()`
  - **Hecho cuando**: Cada función orquesta scanner → analyzer → exporter/comparator; maneja errores (dataset no encontrado → excepción tipada).
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_service.py` — cada operación resuelve dataset → analiza/exporta/compara; `DatasetNotFoundError`, `InvalidArgumentError`, `NoDataInRangeError` tipadas; guard DV-01 (`output_dir` dentro de PARQUET_DIR); métricas RF-8 (`dataset_id`, `duration_ms`, `rows_scanned`, `bytes_read`, `scan_duration_ms`).
- [x] **T4.2**: Tests unitarios `service.py` (mock scanner/analyzer/exporter)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_service.py -v` pasa; cobertura ≥ 90%.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_service.py` pasa; cobertura `service.py` 95%. Se usan datasets Parquet temporales reales en lugar de mocks (más estricto).

## Fase 5: CLI (Typer)

- [x] **T5.1**: Implementar `cli.py` con 9 comandos: `list`, `summary`, `head`, `tail`, `sample`, `gaps`, `anomalies`, `export`, `compare`
  - **Hecho cuando**: `ft_data_viewer --help` muestra 9 subcomandos con help descriptivo.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_cli.py::test_help_lists_nine_commands`.
- [x] **T5.2**: Implementar flags globales: `--verbose/-v`, `--quiet/-q`, `--format [table|json|csv]`, `--log-format [rich|json]`
  - **Hecho cuando**: Tests verifican cada flag afecta comportamiento (nivel log, formato salida).
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_cli.py::test_verbose_and_quiet_set_level`, `test_log_format_json_emits_json_on_stderr`, `test_global_format_json_and_command_override`, `test_list_csv_via_global_format`.
- [x] **T5.3**: Formateo de salida: Rich Table para table, `console.print_json` para json, `write_csv` para csv
  - **Hecho cuando**: Tests verifican salida visual correcta en cada formato.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_cli.py` (`*_table`, `*_json` vía `console.print_json`, `*_csv` vía `pl.DataFrame.write_csv`; csv no soportado en anomalies/compare → exit 2).
- [x] **T5.4**: Exit codes: `0` OK, `1` dataset no encontrado, `2` args inválidos, `3` sin datos en rango, `4` IO error
  - **Hecho cuando**: Tests `pytest -k cli_exit_codes` pasan.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_data_viewer -k cli_exit_codes` pasa (0/1/2/3/4, mapeo por tipo de excepción, sin inspeccionar mensajes).
- [x] **T5.5**: Parse de `--cols` (string coma-separada → List[str]) y `--start/--end` (ISO/epoch → ms)
  - **Hecho cuando**: Tests verifican parsing correcto.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_service.py::test_parse_columns`, `packages/finances-downloader/tests/ft_data_viewer/test_cli.py::test_head_cols_projection_json`, `test_export_start_end_parsing` (ISO y epoch ms vía `finances_core.timeutils.parse_time_input`).

## Fase 6: API (FastAPI)

- [x] **T6.1**: Implementar `api.py` con 7 endpoints REST (ver plan.md §2.2)
  - **Hecho cuando**: `ft_serve` (paquete `finances-api`) levanta; OpenAPI docs en `/docs` muestran endpoints.
  - **Hecho**: `finances_api.create_app()` monta el router; `test_openapi_includes_all_tool_routers` y los tests de `ft_serve` (packages/finances-api/tests/test_app.py) verifican arranque y rutas en OpenAPI.
- [x] **T6.2**: `GET /rows` con query `mode=head|tail|sample`, `n`, `cols`
  - **Hecho cuando**: Test verifica cada modo retorna filas correctas.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_api.py::test_rows_modes`.
- [x] **T6.3**: `POST /export` retorna `StreamingResponse` con headers `Content-Disposition`, `Content-Type` correctos
  - **Hecho cuando**: Test verifica streaming Parquet/CSV/JSONL; archivo descargable.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_api.py::test_export_streams_download` (Parquet/CSV/JSONL, `Content-Type`, `Content-Disposition`), `test_export_with_output_dir_writes_and_streams`, `test_export_temp_file_is_removed`, errores 400/404/422.
- [x] **T6.4**: `POST /compare` con body `CompareRequest` retorna `CompareResult`
  - **Hecho cuando**: Test compara dos datasets vía API.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_api.py::test_compare_endpoint`.
- [x] **T6.5**: Tests de integración API con `httpx.AsyncClient` (testserver)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_api.py -v` pasa.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_api.py::test_async_client_roundtrip` (`httpx.AsyncClient` + `ASGITransport`). (Ruta: `tests/ft_data_viewer/`.)

## Fase 7: Tests de Contrato y Calidad (Art. 7, 17 Constitución)

- [x] **T7.1**: Tests de contrato CLI vs API: mismos inputs → mismos outputs (JSON comparable)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_contract.py -v` pasa.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_contract.py` pasa (list, summary, head, tail, gaps, anomalies, compare, export CSV: CLI JSON == API JSON).
- [x] **T7.2**: Tests de integración CLI end-to-end con dataset Parquet real temporal
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_cli.py -v` pasa.
  - **Hecho**: `pytest packages/finances-downloader/tests/ft_data_viewer/test_cli.py` pasa (CliRunner end-to-end sobre Parquet temporal).
- [x] **T7.3**: Configurar `ruff` + `mypy --strict` en `finances-downloader`
  - **Hecho cuando**: `ruff check packages/finances-downloader` && `mypy --strict packages/finances-downloader` → 0 errors.
  - **Hecho**: `ruff check packages/finances-downloader` → All checks passed; `mypy --strict` del paquete completo → Success (24 archivos); hooks pre-commit locales `mypy-finances-*` + ruff/ruff-format en `.pre-commit-config.yaml`.
- [x] **T7.4**: Cobertura total ≥ 90% (unit + integration + contract)
  - **Hecho cuando**: `pytest --cov=finances_downloader.ft_data_viewer --cov-report=term-missing` ≥ 90%.
  - **Hecho**: `pytest tests/ft_data_viewer --cov=finances_downloader.ft_data_viewer` → TOTAL 96%.

## Fase 8: Documentación y Cierre

- [x] **T8.1**: Actualizar `README.md` sección "2.2 Captura y Gestión de Datos de Mercado" añadiendo `ft_data_viewer`
  - **Hecho cuando**: README refleja descripción y ejemplos copy-pasteables.
  - **Hecho**: README §2.2 describe `ft_data_viewer`.
- [x] **T8.2**: Actualizar `README.md` "Guía de Inicio Rápido" con ejemplos de uso
  - **Hecho cuando**: Comandos `list`, `summary`, `head`, `gaps`, `anomalies`, `export`, `compare` documentados.
  - **Hecho**: README §3 documenta `list`, `summary`, `head`, `gaps`, `anomalies`, `export` y `compare` (flags verificados contra `--help`).
- [x] **T8.3**: Verificar matriz Art. 20 Constitución (100% dimensiones) via checklist
  - **Hecho cuando**: Checklist firmado en PR description; CI pipeline verde.
  - **Hecho**: PR #1 (`refactor/spec-consistency` → `main`) con la checklist del Art. 20 firmada por el Tech Lead (Guillermo Martín Marco, 2026-10-25); CI #3 y #4 en verde en `windows-latest` y `ubuntu-latest`. *Desempeño* = N/A justificado (sin benchmarks aún); métricas VRAM = N/A (sin GPU).
- [x] **T8.4**: Registrar router en `finances-api` (`ft_serve`)
  - **Hecho cuando**: `finances_api.create_app()` importa e incluye `data_viewer_router`.
  - **Hecho**: `finances_api.create_app()` monta el router; `test_openapi_includes_all_tool_routers` y los tests de `ft_serve` (packages/finances-api/tests/test_app.py) verifican arranque y rutas en OpenAPI.
- [x] **T8.5**: Validar que `ft_setup doctor` detecta y lista las 2 nuevas variables `FT_DATA_VIEWER_*`
  - **Hecho cuando**: `ft_setup doctor` output incluye ambas con `source_tool=ft_data_viewer`.
  - **Hecho**: `test_doctor_lists_tool_variables_with_source_tool[ft_data_viewer]` y `test_init_env_template_includes_all_tool_variables` (packages/finances-core/tests/integration/test_setup_cli.py).

## Fase 9: Integración con `ft_market_data_yfinance`

- [x] **T9.1**: `market_type` ampliado (`spot`, `futures`, `equity`, `etf`, `index`, `fx`, `crypto`, `future`, `fund`, `other`) y árboles sin layout de velas ignorados
  - **Hecho cuando**: `list`/`summary` funcionan sobre `yfinance/market_type=equity/symbol=AAPL/timeframe=1d` y `yfinance_events/` no aparece ni genera warnings.
  - **Hecho**: `packages/finances-downloader/tests/ft_data_viewer/test_yfinance_datasets.py` (7 tests) en verde; ayuda `--market` y descripción de la ruta generadas desde `MARKET_TYPES`; `tests/ft_data_viewer` → `187 passed` (ver `specs/ft_market_data_yfinance/tasks.md` T5.1).
