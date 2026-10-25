# Tasks — `ft_indicators` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` (ver CI).
> Tests de `ft_indicators` en `packages/finances-analytics/tests/ft_indicators/`; tests del núcleo en `packages/finances-core/tests/`; sin red y con `FT_PROJECT_ROOT` temporal.
> **Prerrequisito**: la rama `feat/ft-market-data-yfinance` (extracción de `finances_core.marketdata` y `DatasetIdentifier.market_type` ampliado) ya está mergeada en `main`; la rama de trabajo es `feat/ft-indicators` (plan §7).
> **Herramienta hermana**: `ft_ta_lib` (`specs/ft_ta_lib/`) se implementa después y reutiliza el núcleo de la Fase 1 sin cambios de contrato.

## Fase 0: Fundación del Paquete `finances-analytics`

- [x] **T0.0**: Verificar prerrequisito: `feat/ft-indicators` parte de `main` con la rama yfinance mergeada
  - **Hecho cuando**: `from finances_core.marketdata import hive_writer, inventory, plans, tasks_lifecycle` importa y `DatasetIdentifier(market_type="equity", ...)` valida.
  - **Hecho**: `test_foundation.py::test_core_prerequisites_from_yfinance_branch` importa `hive_writer`, `inventory`, `plans`, `tasks_lifecycle` y valida `DatasetIdentifier(market_type="equity")`.
- [ ] **T0.1**: `packages/finances-analytics/pyproject.toml` (deps de plan §5 con `ta-lib>=0.8.2,<0.9` en `[dev]`, script `ft_indicators`, ruff/mypy/pytest como `finances-downloader`, override mypy `talib.*`, marcador `benchmark` excluido por defecto)
  - **Hecho cuando**: `pip install -e "packages/finances-analytics[dev]"` resuelve sobre `finances-core` sin `finances-downloader` e instala la rueda binaria de `ta-lib` en Windows y Ubuntu (cp312); `pip install -e packages/finances-analytics` (sin `[dev]`) no instala `ta-lib`; `ft_indicators --help` responde.
  - **Hecho**: Windows (py3.12): `pip install -e "packages/finances-analytics[dev]"` OK con `ta-lib 0.8.2` (C 0.8.1) en rueda binaria; `pip show` → `Requires` sin `ta-lib` ni `finances-downloader`; `test_talib_is_dev_only_dependency` lo verifica en metadatos; `ft_indicators --help` responde. **Pendiente**: confirmar la rueda en `ubuntu-latest` con la primera ejecución de CI (T0.4).
- [x] **T0.2**: Esqueleto `src/finances_analytics/ft_indicators/` (+ `py.typed`, `kernels/`) y `tests/ft_indicators/conftest.py` con fábrica de datasets Hive sintéticos (binance y yfinance, con y sin `adj_close`)
  - **Hecho cuando**: `test_foundation.py` importa `cli`, `api.router` y la fábrica escribe un dataset legible con `pl.scan_parquet`.
  - **Hecho**: `src/finances_analytics/ft_indicators/{__init__,cli,api}.py`, `kernels/__init__.py`, `py.typed`; `tests/ft_indicators/conftest.py` (fábrica `hive_dataset` binance/yfinance con y sin `adj_close`, escritor Hive del núcleo); `test_foundation.py` (6 tests) verde.
- [x] **T0.3**: Aislamiento Art. 4 (test AST)
  - **Hecho cuando**: test falla si algún módulo de `src/finances_analytics/ft_indicators` importa `finances_downloader`, `finances_api`, otra herramienta `ft_*` (incluida `finances_analytics.ft_ta_lib`), `pandas` o `talib`; `talib` solo se permite bajo `tests/`.
  - **Hecho**: `tests/ft_indicators/test_isolation.py` (11 tests): el comprobador AST detecta `talib`, `pandas`, `finances_downloader`, `finances_api`, `finances_analytics.ft_ta_lib` (absoluto y relativo) y `ft_*`; todos los módulos de `src/finances_analytics/ft_indicators` pasan; `talib` solo se importa en `tests/`.
- [ ] **T0.4**: CI y pre-commit: instalación editable core → downloader → **analytics `[dev]`** → api; `finances-analytics` en el bucle mypy/pytest; hook `mypy-finances-analytics`
  - **Hecho cuando**: CI verde en `windows-latest` y `ubuntu-latest` con el paquete vacío (solo tests de fundación) y `import talib` funcionando en ambos.
  - **Hecho**: `.github/workflows/ci.yml`: instalación core → downloader → `analytics[dev]` → api, paso `import talib`, `finances-analytics` en el bucle mypy/pytest; `.pre-commit-config.yaml`: ruff sobre `finances-analytics` y hook `mypy-finances-analytics`. Local (Windows) verde. **Pendiente**: CI verde en `windows-latest` y `ubuntu-latest` (requiere push).

## Fase 1: Núcleo compartido en `finances_core` (Art. 4, reutilizado por `ft_ta_lib`)

> Extiende lo extraído por la rama yfinance (`finances_core.marketdata`) y crea `finances_core.indicators_store`. Todo se prueba con **dos** espacios de nombres (`ft_indicators` y `ft_ta_lib`) para que `ft_ta_lib` no requiera cambios en el núcleo.

- [x] **T1.1**: `marketdata/hive_writer.py`: parámetros `file_name="data.parquet"` y `mode="merge"|"replace"` (retrocompatibles)
  - **Hecho cuando**: tests nuevos cubren `file_name` arbitrario y `replace` (descarta filas previas del mes); tests de `ft_market_data_binance` y `ft_market_data_yfinance` pasan **sin cambios en sus aserciones**.
  - **Hecho**: `write_parquet_partitioned(..., file_name="data.parquet", mode="merge"|"replace")`; 5 tests nuevos en `finances-core/tests/unit/test_marketdata_hive_writer.py` (nombre arbitrario, `replace` descarta filas previas del mes, orden, `merge` por defecto, modo inválido); tests de `ft_market_data_binance`/`ft_market_data_yfinance` verdes sin tocar sus archivos.
- [x] **T1.2**: `marketdata/plans.py`: parámetro `prefix="download_plan"` en `plan_file_path`/`save_plan`/`load_plan`
  - **Hecho cuando**: test persiste y recupera `share/plans/indicators_plan_{task_id}.json` y `ta_lib_plan_{task_id}.json`; tests de descargadores intactos.
  - **Hecho**: `plan_file_path`/`save_plan`/`load_plan(..., prefix="download_plan")` con validación de prefijo; `test_plan_prefix_roundtrip[indicators_plan|ta_lib_plan]` y rechazo de prefijo inseguro; tests de descargadores intactos.
- [x] **T1.3**: `finances_core/export.py`: mover `stream_export`, `ExportFormat`, `MEDIA_TYPES` desde `ft_data_viewer/exporter.py` y añadir `validate_output_dir(output_dir, forbidden: list[Path])`
  - **Hecho cuando**: tests del núcleo cubren los 3 formatos a archivo y a stream y el rechazo de destinos prohibidos; `ft_data_viewer` importa del núcleo y sus tests pasan sin cambios en aserciones.
  - **Hecho**: `finances_core/export.py` (`stream_export`, `ExportFormat`, `MEDIA_TYPES`, `validate_output_dir`, `ForbiddenOutputDirError`); `test_core_export.py` (14 tests: 3 formatos a archivo y a stream con `Int16`, destinos prohibidos incl. `..`); `ft_data_viewer/exporter.py` eliminado y `api`/`cli`/`service` importan del núcleo; tests de `ft_data_viewer` verdes con solo la línea de import cambiada (el test de aislamiento por archivo pierde el parámetro `exporter.py`: 447 → 446).
- [x] **T1.4**: `DatasetIdentifier.parse("ex/mt/sym/tf")` y `.path_segment()`; `indicators_store/request.py` con `parse_spec_token("name[:k=v,...]")` genérico
  - **Hecho cuando**: tests: round-trip `parse ∘ path_segment`; segmentos faltantes o `market_type` inválido → `ValueError`; `parse_spec_token` acepta enteros, decimales, negativos e identificadores, distingue mayúsculas solo en el nombre y rechaza sintaxis inválida con mensaje claro.
  - **Hecho**: `DatasetIdentifier.parse`/`path_segment` (+ `frozen=True`, hashable) e `indicators_store/request.py` (`parse_spec_token`, `ParsedSpec`, `SpecTokenError`); `test_indicators_request.py` (29 tests: round-trip, segmentos/market_type inválidos, enteros/decimales/negativos/identificadores, mayúsculas solo en el nombre, 12 sintaxis inválidas con mensaje).
- [x] **T1.5**: `schemas/indicators.py`: contratos genéricos (`Stability`, `StoreEntry`, `StoreManifest`, `ComputeProgress`, `ComputeItemResult`, `ComputeResult`, `DispatchResponse`, `SeriesExportRequest`), propios de `ft_indicators` y nomenclatura (`format_param`, `series_id`, `output_columns`)
  - **Hecho cuando**: tests: `rsi`→`rsi_14`; `macd`→`macd_12_26_9_{line,signal,hist}`; `bb:std=2.5`→`bb_20_2p5_*`; `std=2.0`→`2`; `sma:period=50,source=adj_close`→`sma_50_adj_close`; `format_param(-0.5)`→`m0p5`; `series_id("talib_", "bbands", ...)` → `talib_bbands_...`; ids solo `[a-z0-9_]`; specs distintas nunca colisionan (propiedad).
  - **Hecho**: `schemas/indicators.py` (genéricos, propios, constantes Polars y `format_param`/`series_id`/`output_columns`); `test_schemas_indicators.py` (31 tests, incl. ejemplos RF-4, `m0p5`, `talib_bbands_20_2_2_sma`, ids `[a-z0-9_]` y no colisión con Hypothesis).
- [x] **T1.6**: `indicators_store/namespaces.py`: `ft_indicators` (sin prefijo, prohíbe `talib_`) y `ft_ta_lib` (exige `talib_`)
  - **Hecho cuando**: tests: cada namespace rechaza ids del otro; nombres de manifiesto `_manifest.{namespace}.json` y bloqueo `_lock.{namespace}`.
  - **Hecho**: `indicators_store/namespaces.py`; `test_indicators_namespaces.py` (16 tests: cada namespace rechaza los ids del otro e ids inseguros; `_manifest.{ns}.json`, `_lock.{ns}`).
- [x] **T1.7**: `indicators_store/source.py`: `dataset_exists`, `available_columns`, `load_source` (solo columnas pedidas), `validate_source` (Art. 13)
  - **Hecho cuando**: tests: dataset binance y yfinance; `available_columns` lee solo el esquema; `NaN/inf` → nulo con conteo; columna ausente, dtype no canónico u `open_time_ms` duplicado/no creciente → `InvalidSourceError`.
  - **Hecho**: `indicators_store/source.py`; `test_indicators_source.py` (17 tests: binance y yfinance, `available_columns` solo lee el esquema (spy sobre `read/scan_parquet`), `NaN/inf` → nulo con conteo, columna ausente, dtype no canónico, `open_time_ms` duplicado/no creciente/nulo → `InvalidSourceError`).
- [x] **T1.8**: `indicators_store/store.py`: `IndicatorStore.write_series` (reemplazo mensual vía `hive_writer`, huérfanos) y `scan_series`, con dtypes elegidos por la herramienta (`Float32`, `Int16`, `Int64`)
  - **Hecho cuando**: tests: layout `{exchange}/market_type=…/symbol=…/timeframe=…/year=…/month=…/rsi_14.parquet`; cada archivo tiene exactamente las filas fuente del mes; zstd; dtypes respetados; nada se escribe bajo `FT_PARQUET_DIR`; escribir `ema_50` no modifica `rsi_14` (hash); `rsi_14` y `talib_rsi_14` conviven en el mismo mes sin interferir; mes sin filas fuente → archivo eliminado.
  - **Hecho**: `indicators_store/store.py` (`IndicatorStore`, `WriteStats`, `dtype_name`); `test_indicators_store.py` (14 tests: layout `…/year=2024/month=01/rsi_14.parquet`, filas exactas por mes, ZSTD + estadísticas, `Float32`/`Int16`/`Int64`, `Int16` fuera de rango y `Float64` rechazados, `FT_PARQUET_DIR` intacto (hash), `ema_50` no altera `rsi_14` (hash), `rsi_14` y `talib_rsi_14` en el mismo mes, mes sin filas → archivo eliminado).
- [x] **T1.9**: `indicators_store/manifest.py`: manifiesto atómico por namespace (tras las particiones, solo entradas pedidas) y bloqueo `O_EXCL`
  - **Hecho cuando**: tests: caída simulada entre particiones y manifiesto → re-ejecución converge; bloqueo de tarea viva → `DatasetLockedError`; bloqueo de tarea terminal/inexistente → se reemplaza; bloqueos de `ft_indicators` y `ft_ta_lib` sobre el mismo dataset no se excluyen; entradas no pedidas se conservan.
  - **Hecho**: `indicators_store/manifest.py` + `IndicatorStore.update_manifest/lock`; `test_indicators_manifest.py` (12 tests: escritura atómica (fallo de `replace` conserva el anterior), caída entre particiones y manifiesto → la re-ejecución converge a archivos y manifiesto idénticos a una ejecución limpia, bloqueo vivo → `DatasetLockedError`, terminal/inexistente → reemplazado, bloqueos `ft_indicators` y `ft_ta_lib` simultáneos, entradas no pedidas conservadas).
- [x] **T1.10**: `indicators_store/view.py`: `build_wide_view` (rango, series/columnas, `include_ohlcv`, `stability` all/flag/drop, `stale`)
  - **Hecho cuando**: tests: tabla ancha con columnas identidad; `adj_close` solo en yfinance; `flag` añade `{series_id}_is_stable`; `drop` filtra por el mayor `stable_from_ms` y rechaza nulos; `stale` detectado; columnas `Int16`/`Int64` se conservan en la vista.
  - **Hecho**: `indicators_store/view.py` (`build_wide_view`, `WideView`, `ExportValidationError`); `test_indicators_view.py` (11 tests: columnas identidad, rango `[start, end)`, filtro por series/columnas, `adj_close` solo en yfinance, `flag`, `drop` por el mayor `stable_from_ms` y rechazo de nulos, `stale`, `Int16`/`Int64` conservados, series `ft_ta_lib` aisladas).
- [x] **T1.11**: `indicators_store/runner.py`: `run_compute_task` y `dispatch` sobre `tasks_lifecycle` (fases RF-10, semáforo `FT_INDICATORS_MAX_CONCURRENT`, errores por dataset, `FAILED` solo por IO/inesperado)
  - **Hecho cuando**: tests con un `compute_dataset` falso: fases monótonas en `tasks.db`; progreso con `stability`; dataset con `InvalidSourceError` → `COMPLETED` con `errors`; IO simulado → `FAILED` con archivos previos intactos; pico de datasets simultáneos == límite.
  - **Hecho**: `indicators_store/runner.py` (`run_compute_task`, `create_compute_task`, `get_progress`, `get_result`, `dispatch`, `DatasetOutcome`); `test_indicators_runner.py` (10 tests con `compute_dataset` falso: fases monótonas y `COMPLETED` en `tasks.db`, progreso con `stability`, `invalid_source`/`dataset_not_found`/`dataset_locked` → `COMPLETED` con `errors`, `OSError` → `FAILED` con particiones previas intactas, excepción inesperada → `FAILED`, pico de concurrencia == límite para 1/2/3).
- [x] **T1.12**: Settings `INDICATORS_DIR`, `INDICATORS_MAX_CONCURRENT` en `config.py` (+ validación RF-7)
  - **Hecho cuando**: tests: defaults resueltos desde `PROJECT_ROOT`; override por `FT_INDICATORS_DIR`; `INDICATORS_DIR` dentro de `PARQUET_DIR` → error de configuración.
  - **Hecho**: `INDICATORS_DIR` (`./data/indicators`), `INDICATORS_MAX_CONCURRENT` (2, `ge=1, le=16`) y `FinancesSettings.require_indicators_dir()` → `ConfigError`; `test_core_config_indicators.py` (8 tests: defaults desde `PROJECT_ROOT`, override `FT_INDICATORS_DIR`, límites, dentro de `PARQUET_DIR` → error). Registro en `env_list.conf` queda en T7.2.

## Fase 2: Catálogo, Referencias y Kernels

- [ ] **T2.1**: `catalog.py` (`ParamDef`, `IndicatorDef`, `CATALOG`, `resolve`, `talib` equivalente) y `catalog()`
  - **Hecho cuando**: tests: 12 entradas exactas de RF-2; defaults resueltos; parámetro fuera de rango, desconocido, `fast ≥ slow`, `std` con 3 decimales, `source` en indicador OHLC y `adj_close` sin columna → error de validación con detalle; ids sin prefijo `talib_`.
- [ ] **T2.2**: Petición: `--indicator` vía `parse_spec_token` → `IndicatorSpec`, carga de `--request-file`
  - **Hecho cuando**: tests: `macd:fast=12,slow=26,signal=9` produce el mismo `IndicatorSpec` que su JSON; specs duplicadas se deduplican por `indicator_id`.
- [ ] **T2.3**: *Fixtures* publicados en `tests/ft_indicators/fixtures/reference/` (hojas de StockCharts ChartSchool para SMA/EMA, RSI, MACD, Bollinger, ATR, Estocástico, ROC, OBV, ADX; ejemplos de Wilder 1978 para RSI/ATR/ADX; casos calculados a mano para WMA y VWAP móvil)
  - **Hecho cuando**: cada CSV indica su fuente y la tolerancia por redondeo de la publicación; un test verifica que todo indicador del catálogo tiene al menos un *fixture* publicado o calculado a mano.
- [ ] **T2.4**: `kernels/windowed.py`: `sma`, `wma`, `bb`, `roc`, `vwap`, `stoch`
  - **Hecho cuando**: tests contra *fixtures* T2.3 + bordes: calentamiento exacto de RF-2, ventana con nulo → nulo, `roc` con base 0 → nulo, `vwap` con ΣV = 0 → nulo, `stoch` con HH = LL → 0 (TA-Lib), `bb` con σ poblacional; salidas `Float64` del kernel.
- [ ] **T2.5**: `kernels/recursive.py`: `ema`, `rsi`, `macd`, `atr`, `adx` (semillas SMA, suavizado de Wilder)
  - **Hecho cuando**: tests contra *fixtures* + bordes: primera fila no nula en el índice de calentamiento de RF-2 (`rsi_14` en 14, `atr_14` en 14, `adx_14` en 27, `macd_12_26_9_signal` en 33), serie plana → `rsi` 0 (TA-Lib) y `adx` 0, filas nulas excluidas de la recursión con estado mantenido.
- [ ] **T2.6**: `kernels/cumulative.py`: `obv`
  - **Hecho cuando**: tests: OBV₀ = volume₀; subidas/bajadas/iguales; fila nula → nulo sin alterar el acumulado; *fixture* de StockCharts desplazado por la constante inicial.
- [ ] **T2.7**: Contraste directo con TA-Lib en CI: `tests/ft_indicators/test_talib_parity.py` (plan §3.11)
  - **Hecho cuando**: para los 11 indicadores con equivalente, sobre paseo aleatorio, serie plana y precios extremos: kernel `Float64` vs TA-Lib con `rtol=1e-9` desde la primera fila no nula (convención idéntica, mismo índice de calentamiento) o desde el índice de ajuste documentado (`macd` desde 267, `stoch` desde 17, `roc` sin filas de base 0); y salida persistida `Float32` vs TA-Lib dentro de la tabla §3.9 desde `stable_from_ms`; cada ajuste está declarado en `IndicatorDef.talib` y el test falla si un indicador con equivalente no tiene caso.
- [ ] **T2.8**: `convergence.py`: `k_star`, `stable_index`, `stability` (RF-16)
  - **Hecho cuando**: tests analíticos: `k_star(2/21)` = 139 y `ema_20` → índice 158; `k_star(1/14)` = 187 y `rsi_14`/`atr_14` → 201; `adx_14` → ±DI 201, ADX 388; `macd_12_26_9` → line 205, signal/hist 267; (1−α)^k* < 1e-6 ≤ (1−α)^(k*−1) para α aleatorios (propiedad); `obv` → nulo `non_decaying_seed`; no recursivos → primera fila no nula; filas nulas desplazan el índice; serie corta → nulo + `not_converged`.
- [ ] **T2.9**: `precision.py`: tabla de tolerancias `f32` de plan §3.9 publicada en `catalog()`
  - **Hecho cuando**: tests: toda columna del catálogo tiene tolerancia; `catalog()` incluye `stable_from`, `f32_tolerance` y `talib_equivalent`; un test de humo calcula cada indicador en `f32` (Polars `Float32` de punta a punta) sobre el paseo aleatorio y verifica que cae dentro de la tolerancia desde `stable_from` (la tabla es alcanzable).

## Fase 3: Motor, Propiedades y Oráculo (RF-3, RF-17)

- [ ] **T3.1**: Propiedad *sin anticipación* / prefijo estable para todo el catálogo (Hypothesis, series aleatorias con nulos/NaN)
  - **Hecho cuando**: para todo `t`, calcular sobre `[0..t]` da el mismo valor (bit a bit) en `t` que sobre la serie completa; alterar filas `> t` no cambia la salida en `≤ t`.
- [ ] **T3.2**: `engine.py`: recálculo completo en un único `select`, alineación 1:1, determinismo, frontera `Float64`→`Float32`
  - **Hecho cuando**: tests: kernels devuelven `Float64` y lo persistido es `Float32` (cast único en la frontera, test AST/tipo — verificación de RFC-0001); salida con mismas filas y `open_time_ms` que la fuente; calentamiento conservado como nulo; dos cálculos de la misma entrada son idénticos (`assert_frame_equal(check_exact=True)`); historia insuficiente → columna nula + `insufficient_history`.
- [ ] **T3.3**: `oracle.py` + `scripts/export_oracle_fixtures.py`: paquete `fixtures/oracle/v1` generado desde `ft_indicators` (plan §3.10)
  - **Hecho cuando**: el paquete versionado contiene `manifest.json` (schema_version, engine_versions, EPSILON, tolerancias, convenciones, commit, sha256 por archivo), entradas canónicas en Parquet + CSV y salidas `Float64` por caso e indicador con `stable_from_index`; un test regenera el paquete en un directorio temporal y exige igualdad byte a byte de CSV y de contenido Parquet con lo versionado; un test lee los CSV con el módulo `csv` estándar (formato independiente de Polars).

## Fase 4: Planificación

- [ ] **T4.1**: `planner.py`: resolución de specs/columnas + persistencia del plan
  - **Hecho cuando**: tests: plan con un item por (dataset, indicador) deduplicado; recuperable con `load_plan(prefix="indicators_plan")`; `adj_close` sin columna → validación.

## Fase 5: Servicio, CLI y API

- [ ] **T5.1**: `service.py` sobre `indicators_store.runner` + `IndicatorStore("ft_indicators")`
  - **Hecho cuando**: tests: tarea `COMPLETED` con `ComputeItemResult` por par incluyendo `stability`; `GET /status` expone `stability` de los items terminados; manifiesto `_manifest.ft_indicators.json` con `stability` coherente con T2.8; segunda ejecución con la misma fuente → archivos con contenido idéntico y mismo manifiesto salvo `computed_at_ms` (idempotencia RF-8); fuente con velas nuevas → archivos recalculados incluyen las nuevas filas.
- [ ] **T5.2**: `exporter.py` + `service.export` (RF-9) sobre `build_wide_view`
  - **Hecho cuando**: tests: `--indicator` traducido a `series_id`; 3 formatos y stdout; `stability=drop` con `obv` → validación; indicador no calculado → `NotFound`; rango vacío → `EmptyExport`; series de `ft_ta_lib` en el mismo directorio no aparecen en la exportación; destino dentro de `FT_PARQUET_DIR`/`FT_INDICATORS_DIR` → validación.
- [ ] **T5.3**: `cli.py`: `catalog`, `compute` (`--dry-run`, `--request-file`), `plan`, `status`, `result`, `show`, `export`; flags globales; exit codes RF-11
  - **Hecho cuando**: tests CliRunner por comando y por exit code (0/1/2/3/4), incluido dataset inexistente → 1, bloqueado → 1, indicador desconocido → 2, `--stability drop` con `obv` → 2, export vacío → 3, destino no escribible → 4.
- [ ] **T5.4**: `api.py`: rutas de plan §2.2; `POST /compute` 202 en ≤ 200 ms sin cálculo en el endpoint; exportación en *threadpool*
  - **Hecho cuando**: tests con `TestClient`: 202 + UUID v7 + URLs absolutas; *polling* hasta `COMPLETED`; 400/404/409 según RF-12; `GET /result` 409 mientras corre; `StreamingResponse` con `Content-Type` y cabecera `X-FT-Indicators-Stale`.
- [ ] **T5.5**: Contrato CLI ≡ API
  - **Hecho cuando**: `test_contract.py`: JSON de `catalog`/`plan`/`status`/`result`/`show` idéntico en CLI y API; `--request-file` y cuerpo de `POST /compute` producen el mismo `IndicatorComputeRequest` y los mismos archivos.
- [ ] **T5.6**: Logging dual (RF-13)
  - **Hecho cuando**: tests: `--log-format json` emite eventos con `component="ft_indicators"`, `task_id`, `dataset`, `indicator_id`, `rows_per_s`; modo Rich por defecto en CLI.

## Fase 6: Desempeño (RF-14, Art. 20)

- [ ] **T6.1**: `benchmarks/test_bench_indicators.py` (marcador `benchmark`): generador determinista de 1M velas `1m` (semilla fija) y escenarios B1–B2 con los umbrales provisionales
  - **Hecho cuando**: `pytest -m benchmark` imprime la mediana de 5 repeticiones por escenario y falla si supera el umbral vigente (leído de un único lugar, p. ej. `benchmarks/thresholds.json`).
- [ ] **T6.2**: Paso de CI de benchmarks en `ubuntu-latest` (resultados JSON como *artifact*)
  - **Hecho cuando**: el job corre en cada push y publica el *artifact* con las medianas.
- [ ] **T6.3**: Calibración con la primera ejecución real en `ubuntu-latest`
  - **Hecho cuando**: umbrales fijados en `thresholds.json` y en RF-14 como 1,5 × mediana medida (registrada aquí con fecha y run de CI); desde ese commit el job falla ante regresión (verificado con una regresión artificial, p. ej. `sleep` en el motor, que lo hace fallar).

## Fase 7: Integración con la Suite

- [ ] **T7.1**: `finances_api` monta `/indicators` y depende de `finances-analytics`
  - **Hecho cuando**: test de OpenAPI incluye las 7 rutas de `/indicators`; spec/plan/tasks de `finances_api` actualizados.
- [ ] **T7.2**: `ft_setup`: `FT_INDICATORS_DIR`, `FT_INDICATORS_MAX_CONCURRENT` en `env_list.conf(.j2)` y creación de `INDICATORS_DIR` en `init`
  - **Hecho cuando**: `test_doctor_lists_tool_variables_with_source_tool[ft_indicators]` y `test_init_env_template_includes_all_tool_variables` pasan; `ft_setup init` crea `data/indicators`.
- [ ] **T7.3**: README (arquitectura con `finances-analytics`, mapa de utilidades §2.3, inicio rápido con `compute`/`status`/`export` copy-pasteables, bucle de calidad por paquete)
  - **Hecho cuando**: los ejemplos del README se ejecutan tal cual sobre un dataset descargado.

## Fase 8: Cierre

- [ ] **T8.1**: Smoke real (fuera de CI, `FT_PROJECT_ROOT` temporal): descargar BTCUSDT `1h` (binance) y AAPL `1d` (yfinance); `compute` del catálogo completo; repetir `compute` (idempotente); descargar más velas y recalcular; `export --include-ohlcv` a CSV; `ft_serve` expone `/indicators`
  - **Hecho cuando**: salida del smoke registrada en este archivo; la repetición produce archivos con contenido idéntico; el recálculo incluye las velas nuevas.
- [ ] **T8.2**: Verificar matriz Art. 20 vía checklist del PR (Funcional, Contractual, Arquitectural, Datos — RFC-0001 firmado, Concurrencia, Observabilidad, Resiliencia = N/A justificado (sin APIs externas), Calidad ≥ 90 %, **Desempeño** = T6 calibrado, Despliegue)
  - **Hecho cuando**: checklist firmado en la descripción del PR; CI verde (incluido el job de benchmarks y el contraste TA-Lib).
