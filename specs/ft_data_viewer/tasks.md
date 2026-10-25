# Tasks — `ft_data_viewer` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).

## Fase 0: Fundación y Modelos Compartidos

- [ ] **T0.1**: Añadir modelos Pydantic a `packages/finances-core/src/finances_core/schemas/data_viewer.py` (DatasetIdentifier, DatasetInfo, DatasetListResponse, DatasetSummary, ColumnStats, GapInfo, AnomalyReport, ExportRequest, CompareRequest, CompareResult)
  - **Hecho cuando**: Modelos validan ejemplos JSON; MyPy strict pasa sin errors.
- [ ] **T0.2**: Extender `FinancesSettings` en `packages/finances-core/src/finances_core/config.py` con `DATA_VIEWER_SAMPLE_MAX_ROWS`, `DATA_VIEWER_GAP_THRESHOLD_MS`
  - **Hecho cuando**: `FinancesSettings()` instancia con defaults correctos.
- [ ] **T0.3**: Actualizar `packages/finances-core/src/ft_setup/templates/env_list.conf.j2` con 2 nuevas variables (source_tool="ft_data_viewer", since_version="0.1.0")
  - **Hecho cuando**: `ft_setup doctor` muestra las nuevas vars; `ft_setup init` regenera `.env.template`.
- [ ] **T0.4**: Añadir entry point `ft_data_viewer` en `packages/finances-downloader/pyproject.toml` `[project.scripts]`
  - **Hecho cuando**: `pip install -e packages/finances-downloader` instala comando `ft_data_viewer`.
- [ ] **T0.5**: Crear estructura de carpetas `packages/finances-downloader/src/finances_downloader/ft_data_viewer/`
  - **Hecho cuando**: Directorio existe con `__init__.py`.

## Fase 1: Scanner y Metadata (Lectura Lazy)

- [ ] **T1.1**: Implementar `scan_parquet_partitions(parquet_dir: Path) -> List[DatasetInfo]` en `scanner.py`
  - **Hecho cuando**: Test con directorio Parquet temporal particionado (Hive) retorna lista correcta de datasets con year_range, month_count, estimated_rows, size_bytes.
- [ ] **T1.2**: Implementar `read_dataset_metadata(dataset_path: Path) -> Dict[str, ColumnStats]` en `scanner.py`
  - **Hecho cuando**: Test usa `pl.scan_parquet(...).select(pl.all().meta()).collect()` y retorna stats por columna (min, max, mean, null_count, distinct_count) sin leer filas.
- [ ] **T1.3**: Implementar `resolve_dataset_path(base_dir, identifier: DatasetIdentifier) -> Path` en `scanner.py`
  - **Hecho cuando**: Test resuelve path correcto para binance/spot/BTCUSDT/1h.
- [ ] **T1.4**: Tests unitarios `scanner.py` con dataset sintético (múltiples exchanges, symbols, timeframes)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/unit/test_scanner.py -v` pasa; cobertura ≥ 95%.

## Fase 2: Analyzer (Gaps, Anomalies, Stats)

- [ ] **T2.1**: Implementar `compute_summary(lf: pl.LazyFrame, identifier: DatasetIdentifier) -> DatasetSummary` en `analyzer.py`
  - **Hecho cuando**: Test con LazyFrame conocido retorna row_count, date_range, ColumnStats por columna, gaps_detected=0, duplicate_timestamps=0, outlier_count=0.
- [ ] **T2.2**: Implementar `detect_gaps(lf: pl.LazyFrame, timeframe_ms: int, min_gap_candles: int) -> List[GapInfo]` en `analyzer.py`
  - **Hecho cuando**: Test con gaps artificiales (eliminar filas) detecta correctamente gap_start_ms, gap_end_ms, missing_candles, gap_duration_human; respeta `min_gap_candles`.
- [ ] **T2.3**: Implementar `detect_anomalies(lf: pl.LazyFrame) -> AnomalyReport` en `analyzer.py`
  - **Hecho cuando**: Test con dataset sintético conteniendo: 3 timestamps duplicados, 5 nulls en 'close', 2 outliers precio (zscore>3), 10 volumen=0 → reporte correcto en todas las categorías.
- [ ] **T2.4**: Implementar `sample_rows(lf: pl.LazyFrame, n: int, mode: Literal["head","tail","sample"], cols: Optional[List[str]]) -> pl.DataFrame` en `analyzer.py`
  - **Hecho cuando**: `head/tail` usa `sort + limit`; `sample` usa reservoir sampling (Vitter R) sin cargar todo; proyección de columnas funciona.
- [ ] **T2.5**: Tests de integración `analyzer.py` con dataset Parquet real temporal
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/unit/test_analyzer.py -v` pasa; cobertura ≥ 90%.

## Fase 3: Exporter y Comparator

- [ ] **T3.1**: Implementar `stream_export(lf: pl.LazyFrame, format: Literal["parquet","csv","jsonl"], output: Union[Path, IO]) -> int` en `exporter.py`
  - **Hecho cuando**: Test exporta a Parquet/CSV/JSONL streaming (usa `sink_parquet/sink_csv/sink_ndjson`); retorna row count; no OOM en dataset grande simulado.
- [ ] **T3.2**: Implementar `compare_datasets(lf_a: pl.LazyFrame, lf_b: pl.LazyFrame, identifier_a, identifier_b) -> CompareResult` en `comparator.py`
  - **Hecho cuando**: Test con dos datasets idénticos salvo 5 filas con close divergente → price_divergence_max correcto, gaps_only_in_a/b vacíos, overlap correcto.
- [ ] **T3.3**: Tests de integración `exporter.py` + `comparator.py`
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/unit/test_exporter_comparator.py -v` pasa.

## Fase 4: Service Layer (Orquestador Compartido)

- [ ] **T4.1**: Implementar `service.py` con funciones: `list_datasets()`, `get_summary()`, `get_rows()`, `get_gaps()`, `get_anomalies()`, `export_data()`, `compare_datasets()`
  - **Hecho cuando**: Cada función orquesta scanner → analyzer → exporter/comparator; maneja errores (dataset no encontrado → excepción tipada).
- [ ] **T4.2**: Tests unitarios `service.py` (mock scanner/analyzer/exporter)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/unit/test_service.py -v` pasa; cobertura ≥ 90%.

## Fase 5: CLI (Typer)

- [ ] **T5.1**: Implementar `cli.py` con 9 comandos: `list`, `summary`, `head`, `tail`, `sample`, `gaps`, `anomalies`, `export`, `compare`
  - **Hecho cuando**: `ft_data_viewer --help` muestra 9 subcomandos con help descriptivo.
- [ ] **T5.2**: Implementar flags globales: `--verbose/-v`, `--quiet/-q`, `--format [table|json|csv]`, `--log-format [rich|json]`
  - **Hecho cuando**: Tests verifican cada flag afecta comportamiento (nivel log, formato salida).
- [ ] **T5.3**: Formateo de salida: Rich Table para table, `console.print_json` para json, `write_csv` para csv
  - **Hecho cuando**: Tests verifican salida visual correcta en cada formato.
- [ ] **T5.4**: Exit codes: `0` OK, `1` dataset no encontrado, `2` args inválidos, `3` sin datos en rango, `4` IO error
  - **Hecho cuando**: Tests `pytest -k cli_exit_codes` pasan.
- [ ] **T5.5**: Parse de `--cols` (string coma-separada → List[str]) y `--start/--end` (ISO/epoch → ms)
  - **Hecho cuando**: Tests verifican parsing correcto.

## Fase 6: API (FastAPI)

- [ ] **T6.1**: Implementar `api.py` con 7 endpoints REST (ver plan.md §2.2)
  - **Hecho cuando**: `uv run finances-backtester serve` levanta; OpenAPI docs en `/docs` muestran endpoints.
- [ ] **T6.2**: `GET /rows` con query `mode=head|tail|sample`, `n`, `cols`
  - **Hecho cuando**: Test verifica cada modo retorna filas correctas.
- [ ] **T6.3**: `POST /export` retorna `StreamingResponse` con headers `Content-Disposition`, `Content-Type` correctos
  - **Hecho cuando**: Test verifica streaming Parquet/CSV/JSONL; archivo descargable.
- [ ] **T6.4**: `POST /compare` con body `CompareRequest` retorna `CompareResult`
  - **Hecho cuando**: Test compara dos datasets vía API.
- [ ] **T6.5**: Tests de integración API con `httpx.AsyncClient` (testserver)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/test_api.py -v` pasa.

## Fase 7: Tests de Contrato y Calidad (Art. 7, 17 Constitución)

- [ ] **T7.1**: Tests de contrato CLI vs API: mismos inputs → mismos outputs (JSON comparable)
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/contract/ -v` pasa.
- [ ] **T7.2**: Tests de integración CLI end-to-end con dataset Parquet real temporal
  - **Hecho cuando**: `pytest packages/finances-downloader/tests/integration/test_cli.py -v` pasa.
- [ ] **T7.3**: Configurar `ruff` + `mypy --strict` en `finances-downloader`
  - **Hecho cuando**: `ruff check packages/finances-downloader` && `mypy --strict packages/finances-downloader` → 0 errors.
- [ ] **T7.4**: Cobertura total ≥ 90% (unit + integration + contract)
  - **Hecho cuando**: `pytest --cov=finances_downloader.ft_data_viewer --cov-report=term-missing` ≥ 90%.

## Fase 8: Documentación y Cierre

- [ ] **T8.1**: Actualizar `README.md` sección "2.2 Captura y Gestión de Datos de Mercado" añadiendo `ft_data_viewer`
  - **Hecho cuando**: README refleja descripción y ejemplos copy-pasteables.
- [ ] **T8.2**: Actualizar `README.md` "Guía de Inicio Rápido" con ejemplos de uso
  - **Hecho cuando**: Comandos `list`, `summary`, `head`, `gaps`, `anomalies`, `export`, `compare` documentados.
- [ ] **T8.3**: Verificar matriz Art. 20 Constitución (100% dimensiones) via checklist
  - **Hecho cuando**: Checklist firmado en PR description; CI pipeline verde.
- [ ] **T8.4**: Registrar router en `ft_api_service` main app
  - **Hecho cuando**: `ft_api_service` importa e incluye `data_viewer_router`.
- [ ] **T8.5**: Validar que `ft_setup doctor` detecta y lista las 2 nuevas variables `FT_DATA_VIEWER_*`
  - **Hecho cuando**: `ft_setup doctor` output incluye ambas con `source_tool=ft_data_viewer`.