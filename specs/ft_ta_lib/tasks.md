# Tasks — `ft_ta_lib` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` (ver CI).
> Tests en `packages/finances-analytics/tests/ft_ta_lib/`; sin red y con `FT_PROJECT_ROOT` temporal.
> **Prerrequisito**: `ft_indicators` mergeada en `main` (núcleo `finances_core.indicators_store`, `finances_core.export`, `finances_core.schemas.indicators`, runner y `FT_INDICATORS_*`), y RFC-0002 aprobada antes del merge de esta herramienta.

## Fase 0: Fundación

- [ ] **T0.0**: Verificar prerrequisito
  - **Hecho cuando**: `from finances_core.indicators_store import store, manifest, view, runner, request, source` importa en `main` y `IndicatorStore(root, "ft_ta_lib")` acepta ids `talib_*` y rechaza los demás.
- [ ] **T0.1**: `ta-lib>=0.8.2,<0.9` a runtime de `finances-analytics`, `numpy` explícito, script `ft_ta_lib`
  - **Hecho cuando**: `pip install -e packages/finances-analytics` (sin `[dev]`) instala la rueda binaria en Windows y Ubuntu (cp312) y `ft_ta_lib --help` responde.
- [ ] **T0.2**: Esqueleto `src/finances_analytics/ft_ta_lib/` y `tests/ft_ta_lib/conftest.py` (reutiliza la fábrica de datasets sintéticos)
  - **Hecho cuando**: `test_foundation.py` importa `cli` y `api.router`.
- [ ] **T0.3**: Aislamiento Art. 4 (test AST)
  - **Hecho cuando**: test falla si `ft_ta_lib` importa `finances_analytics.ft_indicators`, `finances_downloader`, `finances_api` o `pandas`, o si `talib` se importa fuera de `engine.py`, `catalog.py` y `resolver.py`; el test AST de `ft_indicators` (sin `talib`) sigue verde.

## Fase 1: Contratos

- [ ] **T1.1**: `finances_core/schemas/ta_lib.py` (plan §1.1)
  - **Hecho cuando**: tests: `TaLibComputeRequest` exige al menos `functions` o `groups`; `TaLibSpec.inputs` solo acepta columnas permitidas; ejemplos JSON del plan validan.

## Fase 2: Catálogo y Resolución

- [ ] **T2.1**: `catalog.py`: `build_catalog()` desde `get_function_groups()` + `abstract.Function.info`
  - **Hecho cuando**: tests: el catálogo contiene todas las funciones de `talib.get_functions()` (≈158) con su grupo; 61 en *Pattern Recognition*; `BBANDS` con 3 salidas y `matype`; `MAVP` → `supported=false`, `unavailable_input`; versiones de wrapper y C presentes; una función ficticia añadida a un `talib` simulado aparece sin cambios de código.
- [ ] **T2.2**: Metadatos de parámetros (tipo, default, rango) y tipos de salida (`real`/`pattern`/`category`/`index`)
  - **Hecho cuando**: tests: `RSI.timeperiod` int con default 14 y rango de TA-Lib (si el wrapper lo expone; la vía concreta queda documentada aquí); `CDLENGULFING` → `pattern`/`Int16`; `HT_TRENDMODE` → `category`/`Int16`; `MINMAXINDEX` → `index`/`Int64`; reales → `Float32`.
- [ ] **T2.3**: `resolver.py`: `resolve` + `probe` (RF-3, RF-4, RF-9)
  - **Hecho cuando**: tests: `rsi` → `talib_rsi_14`; `rsi:price=adj_close` → `talib_rsi_14_price_adj_close` y error sin `adj_close`; `bbands:matype=ema` == `bbands:matype=1`; `macd` → `talib_macd_12_26_9_{macd,macdsignal,macdhist}`; `cdlmorningstar` → `talib_cdlmorningstar_0p3`; parámetro inexistente, de tipo erróneo o `TA_BAD_PARAM` → validación con mensaje de TA-Lib; redefinir `prices` → validación; specs distintas nunca colisionan (propiedad sobre el catálogo).
- [ ] **T2.4**: Petición: `--function`/`--group`/`--request-file` → `TaLibComputeRequest`; expansión y deduplicación
  - **Hecho cuando**: tests: `-g "Pattern Recognition"` expande a las 61 funciones soportadas; claves de entrada vs parámetros separadas; > 200 funciones → validación; nombre sin distinción de mayúsculas.

## Fase 3: Motor y Propiedades

- [ ] **T3.1**: `engine.py`: `compute_function` (frontera `float64`, NaN → nulo, nulos en calentamiento de salidas enteras, `Int16` con control de rango, índices → `open_time_ms`, subsecuencia sin nulos)
  - **Hecho cuando**: tests: salida real == `talib` directo sobre los mismos arrays tras `cast` a `Float32` (bit a bit); `CDLHIKKAKE` conserva ±200 en `Int16`; filas `< lookback` nulas en `CDL*`; `MAXINDEX` devuelve el `open_time_ms` del máximo; fila nula en la entrada → nulo en la salida y resto sin `NaN`; valor fuera de `Int16` simulado → `output_out_of_range`; un único punto de conversión desde `float64` (verificación RFC-0002).
- [ ] **T3.2**: Propiedad sin anticipación sobre **todo** el catálogo soportado con defaults (prefijo estable)
  - **Hecho cuando**: para cada función y `t` aleatorios, calcular sobre `[0..t]` da el mismo valor en `t` que sobre la serie completa; las que fallen quedan en `NON_CAUSAL` con el test documentándolas.
- [ ] **T3.3**: Alineación, calentamiento e idempotencia
  - **Hecho cuando**: tests: mismas filas y `open_time_ms` que la fuente para todo el catálogo soportado; dos ejecuciones dan contenido idéntico; historia insuficiente → columna nula + `insufficient_history`; configuración global de TA-Lib intacta tras una tarea.
- [ ] **T3.4**: Estabilidad (RF-17)
  - **Hecho cuando**: tests: `SMA` → primera fila no nula; `EMA`/`RSI`/`KAMA` → nulo con `unstable_period_unknown`; presente en manifiesto, resultado, progreso y catálogo; `stability=drop` sobre `talib_rsi_14` → validación.

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
