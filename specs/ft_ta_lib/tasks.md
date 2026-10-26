# Tasks — `ft_ta_lib` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` (ver CI).
> Tests en `packages/finances-analytics/tests/ft_ta_lib/`; sin red y con `FT_PROJECT_ROOT` temporal.
> **Prerrequisito**: `ft_indicators` mergeada en `main` (núcleo `finances_core.indicators_store`, `finances_core.export`, `finances_core.schemas.indicators`, runner y `FT_INDICATORS_*`), y RFC-0002 aprobada antes del merge de esta herramienta.

## Fase 0: Fundación

- [x] **T0.0**: Verificar prerrequisito
  - **Hecho cuando**: `from finances_core.indicators_store import store, manifest, view, runner, request, source` importa en `main` y `IndicatorStore(root, "ft_ta_lib")` acepta ids `talib_*` y rechaza los demás.
  - **Hecho**: `tests/ft_ta_lib/test_foundation.py::test_core_indicators_store_is_available` importa `store`, `manifest`, `view`, `runner`, `request`, `source`; `test_store_accepts_talib_ids_and_rejects_the_rest` escribe `talib_sma_2` y rechaza `sma_2`, `talib_` y `TALIB_SMA` con `NamespaceViolationError`.
- [x] **T0.1**: `ta-lib>=0.8.2,<0.9` a runtime de `finances-analytics`, `numpy` explícito, script `ft_ta_lib`
  - **Hecho cuando**: `pip install -e packages/finances-analytics` (sin `[dev]`) instala la rueda binaria en Windows y Ubuntu (cp312) y `ft_ta_lib --help` responde.
  - **Hecho**: `ta-lib>=0.8.2,<0.9` y `numpy>=1.26` en `dependencies` (fuera de `[dev]`), script `ft_ta_lib`; `pip install --dry-run -e packages/finances-analytics` (sin `[dev]`) resuelve en Windows py3.12 con la rueda binaria ta-lib 0.8.2 (C 0.8.1) y `ft_ta_lib --help` responde; `test_talib_is_a_runtime_dependency_with_numpy` y `test_ft_ta_lib_console_script_is_declared` lo verifican en metadatos. Ubuntu cp312: rueda manylinux publicada (cp311–cp313); la CI (que instala `finances-analytics`) lo reconfirma. Override mypy `talib.*` con `follow_imports = "skip"` (sin stubs). El test de `ft_indicators` que exigía `ta-lib` solo en `[dev]` pasa a `test_talib_is_declared_and_core_dependencies_are_clean`.
- [x] **T0.2**: Esqueleto `src/finances_analytics/ft_ta_lib/` y `tests/ft_ta_lib/conftest.py` (reutiliza la fábrica de datasets sintéticos)
  - **Hecho cuando**: `test_foundation.py` importa `cli` y `api.router`.
  - **Hecho**: `src/finances_analytics/ft_ta_lib/` (`__init__`, `cli` con `-v/-q/--log-format` y `component="ft_ta_lib"`, `api.router` con prefijo `/ta-lib`); la fábrica de datasets Hive pasa a `tests/datasets.py` + *fixtures* `settings`/`hive_dataset` en `tests/conftest.py`, compartidas con `ft_indicators` (sus tests no cambian); `tests/ft_ta_lib/test_foundation.py::test_cli_and_router_import` verde.
- [x] **T0.3**: Aislamiento Art. 4 (test AST)
  - **Hecho cuando**: test falla si `ft_ta_lib` importa `finances_analytics.ft_indicators`, `finances_downloader`, `finances_api` o `pandas`, o si `talib` se importa fuera de `engine.py`, `catalog.py` y `resolver.py`; el test AST de `ft_indicators` (sin `talib`) sigue verde.
  - **Hecho**: `tests/ft_ta_lib/test_isolation.py`: el checker AST detecta `finances_analytics.ft_indicators` (absoluto y relativo), `finances_downloader`, `finances_api`, `pandas`, `ft_*` y `talib` fuera de `engine.py`/`catalog.py`/`resolver.py`; `test_float64_only_appears_in_engine` (RFC-0002) y `test_ft_ta_lib_sources_respect_isolation` verdes; `tests/ft_indicators/test_isolation.py` (sin `talib`) sigue verde.

## Fase 1: Contratos

- [x] **T1.1**: `finances_core/schemas/ta_lib.py` (plan §1.1)
  - **Hecho cuando**: tests: `TaLibComputeRequest` exige al menos `functions` o `groups`; `TaLibSpec.inputs` solo acepta columnas permitidas; ejemplos JSON del plan validan.
  - **Hecho**: `finances_core/schemas/ta_lib.py`; `packages/finances-core/tests/unit/test_schemas_ta_lib.py` (37 tests): `TaLibComputeRequest` sin `functions` ni `groups` → error; `TaLibSpec.inputs` acepta `open/high/low/close/adj_close/volume` y rechaza el resto; parámetros estrictos (un booleano JSON no pasa a 0/1); el ejemplo de petición de RF-5, plan, catálogo y los nombres de RF-9 validan. Deriva documentada en plan §1.1: `MA_TYPE_NAMES` con los 14 tipos de TA-Lib C 0.8.1.

## Fase 2: Catálogo y Resolución

- [x] **T2.1**: `catalog.py`: `build_catalog()` desde `get_function_groups()` + `abstract.Function.info`
  - **Hecho cuando**: tests: el catálogo contiene todas las funciones de `talib.get_functions()` (≈158) con su grupo; 61 en *Pattern Recognition*; `BBANDS` con 3 salidas y `matype`; `MAVP` → `supported=false`, `unavailable_input`; versiones de wrapper y C presentes; una función ficticia añadida a un `talib` simulado aparece sin cambios de código.
  - **Hecho**: `catalog.py` (`build_catalog`, `build_catalog_from(lib)`, `build_function_table`); `tests/ft_ta_lib/test_catalog.py`: 201 funciones == `talib.get_functions()` con su grupo (el "≈158" de la spec es TA-Lib clásico; C 0.8.1 publica 201), 61 en *Pattern Recognition*, `BBANDS` 3 salidas + `matype`, `MAVP` → `unavailable_input` (única no soportada), versiones `0.8.2`/`0.8.1`, y una función `FAKEOSC` de un `talib` simulado aparece sin cambios de código.
- [x] **T2.2**: Metadatos de parámetros (tipo, default, rango) y tipos de salida (`real`/`pattern`/`category`/`index`)
  - **Hecho cuando**: tests: `RSI.timeperiod` int con default 14 y rango de TA-Lib (si el wrapper lo expone; la vía concreta queda documentada aquí); `CDLENGULFING` → `pattern`/`Int16`; `HT_TRENDMODE` → `category`/`Int16`; `MINMAXINDEX` → `index`/`Int64`; reales → `Float32`.
  - **Hecho**: `tests/ft_ta_lib/test_catalog.py`: `RSI.timeperiod` int, default 14; **rango**: ta-lib 0.8.2 no expone min/max de `TA_GetOptInputParameterInfo` (`_ta_getOptInputParameterInfo`, privado, solo da `name`/`display_name`/`type`/`help`/`default_value`), así que `min`/`max` = `None` y el rango lo valida la ejecución de prueba (`TA_BAD_PARAM`); `CDLENGULFING`/`CDLHIKKAKE` → `pattern`/`Int16`, `HT_TRENDMODE` → `category`/`Int16`, `MINMAXINDEX`/`MAXINDEX`/`MININDEX` → `index`/`Int64`, reales → `Float32`; `MA_TYPE_NAMES` == `talib.MA_Type`. Catálogo: 165 salidas `real`, 61 `pattern`, 4 `category` (`HT_TRENDMODE`, `FRACTAL`×2, `SUPERTREND.trend`), 4 `index`.
- [x] **T2.3**: `resolver.py`: `resolve` + `probe` (RF-3, RF-4, RF-9)
  - **Hecho cuando**: tests: `rsi` → `talib_rsi_14`; `rsi:price=adj_close` → `talib_rsi_14_price_adj_close` y error sin `adj_close`; `bbands:matype=ema` == `bbands:matype=1`; `macd` → `talib_macd_12_26_9_{macd,macdsignal,macdhist}`; `cdlmorningstar` → `talib_cdlmorningstar_0p3`; parámetro inexistente, de tipo erróneo o `TA_BAD_PARAM` → validación con mensaje de TA-Lib; redefinir `prices` → validación; specs distintas nunca colisionan (propiedad sobre el catálogo).
  - **Hecho**: `resolver.py` (`lookup`, `resolve`, `probe`); `tests/ft_ta_lib/test_resolver.py`: `talib_rsi_14`, `talib_rsi_14_price_adj_close` (+ error sin `adj_close`), `bbands:matype=ema` == `matype=1`, `talib_macd_12_26_9_{macd,macdsignal,macdhist}`, `talib_cdlmorningstar_0p3`, parámetro inexistente / tipo erróneo / `TA_BAD_PARAM` (mensaje de TA-Lib vía *lookback* `-1` + probe) → `TaLibValidationError`, redefinir `prices` → validación; propiedad Hypothesis (300 ejemplos) sin colisiones de `series_id`/columnas entre specs canónicas distintas + unicidad de los 200 `series_id` por defecto.
- [x] **T2.4**: Petición: `--function`/`--group`/`--request-file` → `TaLibComputeRequest`; expansión y deduplicación
  - **Hecho cuando**: tests: `-g "Pattern Recognition"` expande a las 61 funciones soportadas; claves de entrada vs parámetros separadas; > 200 funciones → validación; nombre sin distinción de mayúsculas.
  - **Hecho**: `request.py` (`spec_from_token`, `build_request`, `request_from_payload`, `load_request_file`, `group_functions`, `expand`); `tests/ft_ta_lib/test_request.py`: `-g "Pattern Recognition"` → 61 funciones; claves de entrada → `inputs`, resto → `params`; > 200 tras expandir → validación (los 10 grupos completos = exactamente 200 soportadas; +1 → error); nombres de función y grupo sin distinción de mayúsculas; `--request-file` == cuerpo de la API == CLI.

## Fase 3: Motor y Propiedades

- [x] **T3.1**: `engine.py`: `compute_function` (frontera `float64`, NaN → nulo, nulos en calentamiento de salidas enteras, `Int16` con control de rango, índices → `open_time_ms`, subsecuencia sin nulos)
  - **Hecho cuando**: tests: salida real == `talib` directo sobre los mismos arrays tras `cast` a `Float32` (bit a bit); `CDLHIKKAKE` conserva ±200 en `Int16`; filas `< lookback` nulas en `CDL*`; `MAXINDEX` devuelve el `open_time_ms` del máximo; fila nula en la entrada → nulo en la salida y resto sin `NaN`; valor fuera de `Int16` simulado → `output_out_of_range`; un único punto de conversión desde `float64` (verificación RFC-0002).
  - **Hecho**: `engine.py` (`compute_function` → `FunctionResult`); `tests/ft_ta_lib/test_engine.py`: reales == `talib` directo sobre los mismos arrays tras `Float32` (bit a bit en la misma máquina: `RSI`/`KAMA`/`BBANDS`/`ATR`/`HT_DCPERIOD`); `CDLHIKKAKE` conserva ±200 en `Int16`; filas `< lookback` nulas en los 61 `CDL*` y `HT_TRENDMODE`; `MAXINDEX`/`MINMAXINDEX` == `open_time_ms` del extremo de la ventana; fila nula/`NaN` → nulo y resto sin `NaN` (la ventana salta la fila); valor simulado 40000 → `output_out_of_range`; AST: un único `cast(PERSIST_REAL)` en `_persist_real` y `float64` solo en la frontera de entrada (RFC-0002); dtypes persistidos `Float32`/`Int16`/`Int64` para todo el catálogo.
- [x] **T3.2**: Propiedad sin anticipación sobre **todo** el catálogo soportado con defaults (prefijo estable)
  - **Hecho cuando**: para cada función y `t` aleatorios, calcular sobre `[0..t]` da el mismo valor en `t` que sobre la serie completa; las que fallen quedan en `NON_CAUSAL` con el test documentándolas.
  - **Hecho**: `tests/ft_ta_lib/test_properties.py`: prefijo estable (bit a bit) para las 200 funciones soportadas con defaults, `t` deterministas (0…398, incl. *lookback* máx. 100 de `PERCENTRANK`) y Hypothesis (series de 1–180 filas con nulos/`NaN`, `t` aleatorio) + alterar filas `> t` no cambia el pasado; **ninguna** función falla con ta-lib 0.8.2 / C 0.8.1 (incl. `FRACTAL` y `DPO`) → `NON_CAUSAL` vacío y documentado; un test de control detecta una salida con anticipación simulada.
- [x] **T3.3**: Alineación, calentamiento e idempotencia
  - **Hecho cuando**: tests: mismas filas y `open_time_ms` que la fuente para todo el catálogo soportado; dos ejecuciones dan contenido idéntico; historia insuficiente → columna nula + `insufficient_history`; configuración global de TA-Lib intacta tras una tarea.
  - **Hecho**: `tests/ft_ta_lib/test_alignment.py`: mismas filas y `open_time_ms` que la fuente para todo el catálogo (0, 1, 5 y 300 filas, con nulos); dos ejecuciones → marcos idénticos y archivos Parquet con el mismo SHA-256 vía `IndicatorStore("ft_ta_lib")`; historia insuficiente → columnas nulas + `insufficient_history`; `engine.global_config()` (compatibilidad + *unstable period* de las 23 funciones con el *flag*) igual a 0 antes y después de calcular y escribir todo el catálogo.
- [x] **T3.4**: Estabilidad (RF-17)
  - **Hecho cuando**: tests: `SMA` → primera fila no nula; `EMA`/`RSI`/`KAMA` → nulo con `unstable_period_unknown`; presente en manifiesto, resultado, progreso y catálogo; `stability=drop` sobre `talib_rsi_14` → validación.
  - **Hecho**: `engine.stability`; `tests/ft_ta_lib/test_stability.py`: `SMA` → primera fila no nula (`first_non_null`); `EMA`/`RSI`/`KAMA`/`HT_TRENDMODE`/`MAMA` → `stable_from_ms` nulo + `unstable_period_unknown`; el catálogo publica `stability_rule`; una tarea real del runner compartido (`run_compute_task`, `plan_prefix="ta_lib_plan"`) con un `compute_dataset` mínimo de test sobre motor + `IndicatorStore` deja el `Stability` en `_manifest.ft_ta_lib.json`, `get_progress` y `get_result`; `build_wide_view(stability="drop")` sobre `talib_rsi_14` → `ExportValidationError` (el servicio de T4.1/T4.2 reutiliza estas mismas piezas).

## Fase 4: Servicio, CLI y API

- [ ] **T4.1**: `planner.py` + `service.py` sobre el runner y `IndicatorStore("ft_ta_lib")`
  - **Hecho cuando**: tests: tarea `COMPLETED` con `ComputeItemResult` por par; manifiesto `_manifest.ft_ta_lib.json` con versión de TA-Lib; archivos `talib_*.parquet` junto a los de `ft_indicators` sin modificarlos (hash); excepción de TA-Lib en un item → `errors` y la tarea sigue; IO simulado → `FAILED`; plan recuperable con `prefix="ta_lib_plan"`.
- [ ] **T4.2**: `exporter.py` + `service.export`
  - **Hecho cuando**: tests: 3 formatos y stdout; columnas `Int16`/`Int64` conservan su tipo en Parquet; solo series `talib_*`; no calculado → 404/exit 1; rango vacío → 400/exit 3.
- [ ] **T4.3**: `cli.py`: `catalog`, `compute` (`--dry-run`), `plan`, `status`, `result`, `show`, `export`; exit codes
  - **Hecho cuando**: tests CliRunner por comando y por exit code (0/1/2/3/4): dataset inexistente → 1, bloqueado → 1, función desconocida / no soportada / `TA_BAD_PARAM` → 2, export vacío → 3, destino no escribible → 4.
- [ ] **T4.4**: `api.py`: rutas de plan §2.2; `POST /compute` 202 en ≤ 200 ms; validación con `probe` en *threadpool*
  - **Hecho cuando**: tests con `TestClient`: 202 + UUID v7 + URLs absolutas; *polling* hasta `COMPLETED`; 400/404/409; `GET /catalog?group=Pattern Recognition` filtra.
- [ ] **T4.5**: Contrato CLI ≡ API y logging dual
  - **Hecho cuando**: `test_contract.py`: JSON de `catalog`/`plan`/`status`/`result`/`show` idéntico; `--request-file` == cuerpo de la API; logs JSON con `component="ft_ta_lib"`, `series_id`, `function`, `group`.

## Fase 5: Desempeño (RF-15)

- [ ] **T5.1**: `benchmarks/test_bench_ta_lib.py`: escenarios B1–B3 sobre el generador de 1M velas compartido
  - **Hecho cuando**: `pytest -m benchmark` imprime la mediana de 5 repeticiones y falla si supera el umbral de `thresholds.json`.
- [ ] **T5.2**: Calibración en `ubuntu-latest` y umbral de regresión
  - **Hecho cuando**: umbrales = 1,5 × mediana registrados aquí y en RF-15 con fecha y run de CI; una regresión artificial hace fallar el job.

## Fase 6: Integración con la Suite

- [ ] **T6.1**: `finances_api` monta `/ta-lib`
  - **Hecho cuando**: test de OpenAPI incluye las 7 rutas de `/ta-lib`; spec/plan/tasks de `finances_api` actualizados.
- [ ] **T6.2**: Validación cruzada `ft_indicators` ↔ `ft_ta_lib` en `packages/finances-api/tests/test_cross_validation_indicators.py` (plan §3.5, solo HTTP)
  - **Hecho cuando**: para cada indicador de `GET /indicators/catalog` con `talib_equivalent`, las columnas exportadas por ambas herramientas coinciden dentro de la tolerancia publicada desde el punto de comparación declarado; el test falla si aparece un `talib_equivalent` sin comparar; ningún módulo del test importa `finances_analytics`.
- [ ] **T6.3**: `env_list.conf(.j2)` (descripción de variables compartidas) y README (`ft_ta_lib catalog/compute/export` copy-pasteables)
  - **Hecho cuando**: `ft_setup doctor` sigue verde; los ejemplos del README se ejecutan tal cual.

## Fase 7: Cierre (Art. 20)

- [ ] **T7.1**: Smoke real: BTCUSDT `1h` y AAPL `1d`; `compute -g "Pattern Recognition" -f kama -f ht_trendline`; repetir (idempotente); `export --include-ohlcv` a CSV; `ft_serve` expone `/ta-lib`
  - **Hecho cuando**: salida registrada en este archivo; la repetición produce contenido idéntico.
- [ ] **T7.2**: Matriz Art. 20 en el checklist del PR (Funcional, Contractual, Arquitectural — Art. 4 con `ft_indicators`, Datos — RFC-0002 aprobada, Concurrencia, Observabilidad, Resiliencia = N/A justificado, Calidad ≥ 90 %, Desempeño = T5 calibrado, Despliegue)
  - **Hecho cuando**: checklist firmado en la descripción del PR; CI verde (incluida la validación cruzada).
