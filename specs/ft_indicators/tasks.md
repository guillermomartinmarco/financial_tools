# Tasks — `ft_indicators` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` (ver CI).
> Tests de `ft_indicators` en `packages/finances-analytics/tests/ft_indicators/`; sin red y con `FT_PROJECT_ROOT` temporal.
> **Prerrequisito**: la rama `feat/ft-market-data-yfinance` (extracción de `finances_core.marketdata` y `DatasetIdentifier.market_type` ampliado) debe estar mergeada en `main` antes de empezar (plan §7).

## Fase 0: Fundación del Paquete `finances-analytics`

- [ ] **T0.0**: Verificar prerrequisito: rama creada desde `main` con la rama yfinance ya mergeada
  - **Hecho cuando**: `from finances_core.marketdata import hive_writer, inventory, plans, tasks_lifecycle` importa y `DatasetIdentifier(market_type="equity", ...)` valida en `main`.
- [ ] **T0.1**: `packages/finances-analytics/pyproject.toml` (deps de plan §5, script `ft_indicators`, ruff/mypy/pytest como `finances-downloader`, marcador `benchmark` excluido por defecto, `scripts/` fuera de mypy/pytest)
  - **Hecho cuando**: `pip install -e "packages/finances-analytics[dev]"` resuelve sobre `finances-core` sin instalar `finances-downloader` ni TA-Lib; `ft_indicators --help` responde.
- [ ] **T0.2**: Esqueleto `src/finances_analytics/ft_indicators/` (+ `py.typed`, `kernels/`) y `tests/ft_indicators/conftest.py` con fábrica de datasets Hive sintéticos (binance y yfinance, con y sin `adj_close`)
  - **Hecho cuando**: `test_foundation.py` importa `cli`, `api.router` y la fábrica escribe un dataset legible con `pl.scan_parquet`.
- [ ] **T0.3**: Aislamiento Art. 4 (test AST)
  - **Hecho cuando**: test falla si algún módulo de `src/finances_analytics` importa `finances_downloader`, `finances_api`, otro `ft_*`, `pandas` o `talib`.
- [ ] **T0.4**: CI y pre-commit: instalación editable core → downloader → **analytics** → api; `finances-analytics` en el bucle mypy/pytest; hook `mypy-finances-analytics`
  - **Hecho cuando**: CI verde en `windows-latest` y `ubuntu-latest` con el paquete vacío (solo tests de fundación).

## Fase 1: Núcleo compartido en `finances_core` (Art. 4)

> Parte de lo extraído por la rama yfinance (`finances_core.marketdata`, `DatasetIdentifier.market_type`); esta fase solo lo extiende.

- [ ] **T1.1**: `marketdata/hive_writer.py`: parámetros `file_name="data.parquet"` y `mode="merge"|"replace"` (retrocompatibles)
  - **Hecho cuando**: tests nuevos cubren `file_name` arbitrario y `replace` (descarta filas previas del mes); tests de `ft_market_data_binance` y `ft_market_data_yfinance` pasan **sin cambios en sus aserciones**.
- [ ] **T1.2**: `marketdata/plans.py`: parámetro `prefix="download_plan"` en `plan_file_path`/`save_plan`/`load_plan`
  - **Hecho cuando**: test persiste y recupera `share/plans/indicators_plan_{task_id}.json`; tests de descargadores intactos.
- [ ] **T1.3**: `finances_core/export.py`: mover `stream_export`, `ExportFormat`, `MEDIA_TYPES` desde `ft_data_viewer/exporter.py` y añadir `validate_output_dir(output_dir, forbidden: list[Path])`
  - **Hecho cuando**: tests del núcleo cubren los 3 formatos a archivo y a stream y el rechazo de destinos prohibidos; `ft_data_viewer` importa del núcleo y sus tests pasan sin cambios en aserciones.
- [ ] **T1.4**: `DatasetIdentifier.parse("ex/mt/sym/tf")` y `.path_segment()`
  - **Hecho cuando**: tests: round-trip `parse ∘ path_segment`; segmentos faltantes o `market_type` inválido → `ValueError`.
- [ ] **T1.5**: `schemas/indicators.py`: contratos de plan §1.1 (incluido `Stability`) + `format_param`, `indicator_id`, `output_columns` (RF-4)
  - **Hecho cuando**: tests: `rsi`→`rsi_14`; `macd`→`macd_12_26_9_{line,signal,hist}`; `bb:std=2.5`→`bb_20_2p5_*`; `std=2.0`→`2`; `sma:period=50,source=adj_close`→`sma_50_adj_close`; ids solo `[a-z0-9_]`; specs distintas nunca colisionan (propiedad).
- [ ] **T1.6**: Settings `INDICATORS_DIR`, `INDICATORS_MAX_CONCURRENT` en `config.py` (+ validación RF-7)
  - **Hecho cuando**: tests: defaults resueltos desde `PROJECT_ROOT`; override por `FT_INDICATORS_DIR`; `INDICATORS_DIR` dentro de `PARQUET_DIR` → error de configuración.

## Fase 2: Catálogo, Petición, Referencias y Kernels

- [ ] **T2.1**: `catalog.py` (`ParamDef`, `IndicatorDef`, `CATALOG`, `resolve`) y `catalog()`
  - **Hecho cuando**: tests: 12 entradas exactas de RF-2; defaults resueltos; parámetro fuera de rango, desconocido, `fast ≥ slow`, `std` con 3 decimales, `source` en indicador OHLC y `adj_close` sin columna → error de validación con detalle.
- [ ] **T2.2**: `spec_parser.py` (gramática `name[:k=v,...]`) y carga de `--request-file`
  - **Hecho cuando**: tests: `macd:fast=12,slow=26,signal=9` produce el mismo `IndicatorSpec` que su JSON; sintaxis inválida → error claro; specs duplicadas se deduplican por `indicator_id`.
- [ ] **T2.3**: *Fixtures* publicados en `tests/ft_indicators/fixtures/reference/` (hojas de StockCharts ChartSchool para SMA/EMA, RSI, MACD, Bollinger, ATR, Estocástico, ROC, OBV, ADX; ejemplos de Wilder 1978 para RSI/ATR/ADX; casos calculados a mano para WMA y VWAP móvil)
  - **Hecho cuando**: cada CSV tiene cabecera de procedencia (fuente, URL o cita, parámetros, redondeo de la fuente, tolerancia); un test verifica que todo indicador del catálogo tiene al menos un *fixture*.
- [ ] **T2.4**: *Fixtures* TA-Lib generados **offline**: `packages/finances-analytics/scripts/generate_talib_fixtures.py` (entorno aparte con TA-Lib; no es dependencia del paquete ni de CI) sobre una serie OHLCV sintética fija versionada, para todos los indicadores con equivalente TA-Lib (`SMA`, `EMA`, `WMA`, `RSI`, `MACD`, `BBANDS`, `ATR`, `STOCH`, `ROC`, `OBV`, `ADX`, `PLUS_DI`, `MINUS_DI`)
  - **Hecho cuando**: los CSV generados quedan versionados en `fixtures/reference/talib_*.csv` con cabecera de procedencia (versión de TA-Lib y del *wrapper*, fecha, comando, parámetros, sha256 de la serie de entrada); CI los lee sin importar `talib`; las diferencias de convención documentadas en RF-2 (MACD con semillas propias, inicio de %K y ±DI) se comparan solo a partir del índice indicado en la cabecera.
- [ ] **T2.5**: `kernels/windowed.py`: `sma`, `wma`, `bb`, `roc`, `vwap`, `stoch`
  - **Hecho cuando**: tests contra *fixtures* T2.3/T2.4 (tolerancia declarada) + bordes: calentamiento exacto de RF-2, ventana con nulo → nulo, `roc` con base 0 → nulo, `vwap` con ΣV = 0 → nulo, `stoch` con HH = LL → 0 (TA-Lib), `bb` con σ poblacional; salidas `Float32`.
- [ ] **T2.6**: `kernels/recursive.py`: `ema`, `rsi`, `macd`, `atr`, `adx` (semillas SMA, suavizado de Wilder)
  - **Hecho cuando**: tests contra *fixtures* + bordes: primera fila no nula en el índice de calentamiento de RF-2 (`rsi_14` en 14, `atr_14` en 14, `adx_14` en 27, `macd_12_26_9_signal` en 33), serie plana → `rsi` 0 (TA-Lib) y `adx` 0, filas nulas excluidas de la recursión con estado mantenido.
- [ ] **T2.7**: `kernels/cumulative.py`: `obv`
  - **Hecho cuando**: tests: OBV₀ = volume₀ (coincide con *fixture* TA-Lib); subidas/bajadas/iguales; fila nula → nulo sin alterar el acumulado; *fixture* de StockCharts desplazado por la constante inicial.

- [ ] **T2.8**: `convergence.py`: `k_star`, `stable_index`, `stability` (RF-16)
  - **Hecho cuando**: tests analíticos: `k_star(2/21)` = 139 y `ema_20` → índice 158; `k_star(1/14)` = 187 y `rsi_14`/`atr_14` → 201; `adx_14` → ±DI 201, ADX 388; `macd_12_26_9` → line 205, signal/hist 267; (1−α)^k* < 1e-6 ≤ (1−α)^(k*−1) para α aleatorios (propiedad); `obv` → nulo `non_decaying_seed`; no recursivos → primera fila no nula; filas nulas desplazan el índice; serie corta → nulo + `not_converged`.
- [ ] **T2.9**: `precision.py`: tabla de tolerancias `f32` de plan §3.9 publicada en `catalog()`
  - **Hecho cuando**: tests: toda columna del catálogo tiene tolerancia; `GET /catalog`-ready `catalog()` incluye `stable_from` y `f32_tolerance`; un test de humo calcula cada indicador en `f32` (Polars `Float32` de punta a punta) sobre el caso de paseo aleatorio y verifica que cae dentro de la tolerancia desde `stable_from` (valida que la tabla es alcanzable).

## Fase 3: Motor y Propiedades (RF-3)

- [ ] **T3.1**: Propiedad *sin anticipación* / prefijo estable para todo el catálogo (Hypothesis, series aleatorias con nulos/NaN)
  - **Hecho cuando**: para todo `t`, calcular sobre `[0..t]` da el mismo valor (bit a bit) en `t` que sobre la serie completa; alterar filas `> t` no cambia la salida en `≤ t`.
- [ ] **T3.2**: `engine.py`: recálculo completo en un único `select`, alineación 1:1 y determinismo
  - **Hecho cuando**: tests: kernels devuelven `Float64` y lo persistido es `Float32` (cast único en la frontera, test AST/tipo); salida con mismas filas y `open_time_ms` que la fuente; calentamiento conservado como nulo; dos cálculos de la misma entrada son idénticos (`assert_frame_equal(check_exact=True)`); historia insuficiente → columna nula + `insufficient_history`; todas las salidas `Float32`.
- [ ] **T3.3**: Paralelismo entre datasets (`asyncio.to_thread` + semáforo `FT_INDICATORS_MAX_CONCURRENT`)
  - **Hecho cuando**: test: pico de datasets simultáneos == límite; resultados idénticos con concurrencia 1 y 4.

- [ ] **T3.4**: `oracle.py` + `scripts/export_oracle_fixtures.py`: paquete `fixtures/oracle/v1` (plan §3.10)
  - **Hecho cuando**: el paquete versionado contiene `manifest.json` (schema_version, engine_versions, EPSILON, tolerancias, convenciones, commit, sha256 por archivo), entradas canónicas en Parquet + CSV y salidas `Float64` por caso e indicador con `stable_from_index`; un test regenera el paquete en un directorio temporal y exige igualdad byte a byte de CSV y de contenido Parquet con lo versionado; un test lee los CSV sin Polars (módulo `csv` estándar) para probar la independencia de Python/Polars del formato.

## Fase 4: Fuente, Almacenamiento y Planificación

- [ ] **T4.1**: `source.py`: `load_source` (solo columnas necesarias) y `validate_source` (Art. 13)
  - **Hecho cuando**: tests: dataset binance y yfinance; `NaN/inf` → nulo con conteo; columna ausente, dtype no canónico o `open_time_ms` duplicado/no creciente → `invalid_source`.
- [ ] **T4.2**: `store.py`: reescritura `{indicator_id}.parquet` por mes vía `hive_writer` (`mode="replace"`) y borrado de huérfanos
  - **Hecho cuando**: tests: layout `{exchange}/market_type=…/symbol=…/timeframe=…/year=…/month=…/rsi_14.parquet`; dtypes `Int64`/`Float32`, zstd; cada archivo tiene exactamente las filas fuente del mes; nada se escribe bajo `FT_PARQUET_DIR`; calcular `ema_50` no modifica los archivos de `rsi_14` (hash); mes que deja de existir en la fuente → su archivo se elimina.
- [ ] **T4.3**: Manifiesto `_manifest.json` (atómico, tras las particiones, solo entradas pedidas) y bloqueo `_lock`
  - **Hecho cuando**: tests: el manifiesto contiene `stability` (`stable_from_ms` por indicador y por columna, regla y motivo) coherente con T2.8; caída simulada entre particiones y manifiesto → re-ejecución converge al mismo resultado; bloqueo de tarea viva → `DatasetLockedError`; bloqueo de tarea terminal/inexistente → se reemplaza; entradas de indicadores no pedidos se conservan.
- [ ] **T4.4**: `planner.py`: resolución de specs/columnas + persistencia del plan
  - **Hecho cuando**: tests: plan con un item por (dataset, indicador) deduplicado; recuperable con `load_plan(prefix="indicators_plan")`; `adj_close` sin columna → validación.

## Fase 5: Servicio, CLI y API

- [ ] **T5.1**: `service.py`: orquestador con fases RF-10 sobre `TaskRecorder`
  - **Hecho cuando**: tests: tarea `COMPLETED` con `IndicatorItemResult` por par incluyendo `stability`; `GET /status` expone `stability` de los items terminados; segunda ejecución con la misma fuente → archivos con contenido idéntico y mismo manifiesto salvo `computed_at_ms` (idempotencia RF-8); fuente con velas nuevas → archivos recalculados incluyen las nuevas filas; dataset con `invalid_source` → `COMPLETED` con `errors`; error de IO simulado → `FAILED` con archivos previos intactos; fases monótonas persistidas en `tasks.db`.
- [ ] **T5.2**: `exporter.py` + `service.export` (RF-9)
  - **Hecho cuando**: tests: tabla ancha con columnas identidad, `include_ohlcv` (con `adj_close` solo en yfinance), filtros de rango/indicadores/columnas, 3 formatos, stdout; `stability=flag` añade `{indicator_id}_is_stable` (falso antes de `stable_from_ms`), `stability=drop` elimina las filas anteriores al mayor `stable_from_ms` y con `obv` → validación; indicador no calculado → `NotFound`; rango vacío → `EmptyExport`; fuente con velas posteriores a `last_ms` → warning `stale`; destino dentro de `FT_PARQUET_DIR`/`FT_INDICATORS_DIR` → validación.
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
- [ ] **T8.2**: Verificar matriz Art. 20 vía checklist del PR (Funcional, Contractual, Arquitectural, Datos, Concurrencia, Observabilidad, Resiliencia = N/A justificado (sin APIs externas), Calidad ≥ 90 %, **Desempeño** = T6 calibrado, Despliegue)
  - **Hecho cuando**: checklist firmado en la descripción del PR; CI verde (incluido el job de benchmarks).
