# Spec — `ft_ta_lib` (MVP v1.0)

## Contexto e Historias de Usuario

`ft_indicators` ofrece 12 indicadores propios en Polars, validados y con rol de oráculo. Muchos análisis necesitan
además el resto del catálogo clásico: ~158 funciones de TA-Lib (medias KAMA/T3/MAMA, transformadas de Hilbert,
estadísticas, operadores matemáticos y 61 patrones de velas). `pip install ta-lib` instala hoy ruedas binarias
(ta-lib 0.8.2, TA-Lib C 0.8.1) para Windows y manylinux x86_64 (cp312), y `talib.abstract` /
`talib.get_function_groups()` describen el catálogo completo. `ft_ta_lib` es una envoltura delgada sobre **todo** ese
catálogo con **el mismo contrato de datos** que `ft_indicators`: lee los datasets Hive de los descargadores, alinea
1:1 por `open_time_ms`, persiste por función y mes bajo `FT_INDICATORS_DIR` y exporta a Parquet / CSV / JSONL.
Vive en el paquete `finances-analytics` junto a `ft_indicators`, sin importarla (Art. 4): todo lo común está en
`finances_core` (ver `specs/ft_indicators/plan.md` §1.0).

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** calcular cualquier función de TA-Lib (p. ej. `KAMA`, `HT_TRENDLINE`, `CDLENGULFING`) sobre mis datasets **para que** no tenga que reimplementar indicadores que ya existen. |
| US-2 | Quant Researcher | Como investigador, **quiero** ver qué grupos, funciones, parámetros, entradas y salidas existen **para que** sepa qué puedo pedir sin leer el código de TA-Lib. |
| US-3 | Backtest Engineer | Como autor del backtester, **exijo** el mismo contrato que `ft_indicators` (alineación 1:1, calentamiento nulo, sin anticipación, nombres deterministas) **para que** pueda mezclar columnas de ambas herramientas con *joins* exactos. |
| US-4 | Analista externo | Como usuario de Excel / R / DuckDB, **quiero** exportar los resultados (con OHLCV opcional) **para que** pueda usarlos fuera de la suite. |
| US-5 | Operador | Como operador, **quiero** `ft_ta_lib compute` y `POST /ta-lib/compute` con el contrato de despacho y *polling* de la suite **para que** pueda lanzar cálculos desde la terminal o desde `ft_serve`. |

---

## Requisitos Funcionales (EARS)

### RF-1 — Pipeline de Cálculo Determinista (Ubícuo)
**EL SISTEMA DEBE** ejecutar, para cada dataset de la petición:
1. **Validar Petición** (RF-4, RF-5): funciones del catálogo y soportadas, parámetros válidos, entradas disponibles. **SI** algo es inválido, **ENTONCES** exit 2 / HTTP 400 con el detalle; **SI** un dataset no existe, **ENTONCES** exit 1 / HTTP 404.
2. **Planificar**: expandir grupos, resolver defaults, `series_id` y columnas de cada (dataset, función) y **persistir el plan** en `<raíz del proyecto>/share/plans/ta_lib_plan_{task_id}.json`.
3. **Cargar** la serie fuente **completa** (solo `open_time_ms` y las columnas de entrada necesarias) y validarla (Art. 13).
4. **Calcular** cada función sobre la serie completa (**recálculo completo** en cada ejecución) con el motor de TA-Lib.
5. **Reescribir** los archivos de cada función pedida (RF-8), actualizar el manifiesto y **cerrar la tarea** (RF-11).

La fuente es de **solo lectura**: **EL SISTEMA NO DEBE** escribir nunca bajo `FT_PARQUET_DIR`.

### RF-2 — Catálogo Dinámico (Ubícuo)
- **EL SISTEMA DEBE** construir el catálogo **en tiempo de ejecución** a partir de `talib.get_function_groups()` y `talib.abstract.Function(name).info`, de modo que las funciones nuevas de una versión de TA-Lib aparezcan sin cambios de código.
- Cada entrada **DEBE** incluir: grupo, nombre, nombre legible, *flags* de TA-Lib (p. ej. *unstable period*, *candlestick*), entradas con sus columnas por defecto, parámetros con tipo, default y rango (cuando TA-Lib lo publica), salidas con su tipo persistido (RF-7), *lookback* con defaults, y `supported` + `unsupported_reason`.
- `ft_ta_lib catalog [--group G] [--function F]` y `GET /ta-lib/catalog?group=&function=` **DEBEN** devolver el mismo JSON.
- **SI** una función requiere una entrada que el esquema OHLCV no tiene (p. ej. `MAVP` con `periods`), **ENTONCES** figura con `supported=false`, `unsupported_reason="unavailable_input"`.
- **SI** una función no supera el test de causalidad (RF-6), **ENTONCES** figura con `supported=false`, `unsupported_reason="non_causal"`.
- La versión de TA-Lib (wrapper y C) **DEBE** figurar en el catálogo y en el manifiesto.

### RF-3 — Mapeo de Entradas (Ubícuo)
- Las entradas de TA-Lib se mapean a las columnas canónicas homónimas: `open`, `high`, `low`, `close`, `volume`.
- Las entradas de **una serie** (`price`, `price0`, `price1`) aceptan una columna alternativa en {`open`, `high`, `low`, `close`, `adj_close`, `volume`} mediante la clave del nombre de entrada (p. ej. `price=adj_close`); el default es el de TA-Lib. Las entradas compuestas (`prices`: p. ej. high/low/close) usan siempre las columnas crudas (misma razón que `ft_indicators` RF-6).
- **SI** se pide `adj_close` sobre un dataset sin esa columna, **ENTONCES** exit 2 / HTTP 400.
- **SI** se intenta redefinir una entrada compuesta o una entrada inexistente, **ENTONCES** exit 2 / HTTP 400.

### RF-4 — Parámetros y Validación (Ubícuo)
- **EL SISTEMA DEBE** validar los parámetros con los metadatos de TA-Lib: nombre existente, tipo (entero o real), y rango publicado por TA-Lib cuando exista.
- Parámetros de tipo media móvil (`matype`, `slowk_matype`, `fastd_matype`, …) aceptan el nombre (`sma`, `ema`, `wma`, `dema`, `tema`, `trima`, `kama`, `mama`, `t3`) o su entero 0–8 (`talib.MA_Type`).
- **SI** TA-Lib rechaza una combinación de parámetros (`TA_BAD_PARAM`), **ENTONCES** exit 2 / HTTP 400 con el mensaje de TA-Lib; la validación se hace antes de despachar, sobre una serie sintética mínima.

### RF-5 — Petición (Ubícuo)
**EL SISTEMA DEBE** aceptar una única forma canónica, `TaLibComputeRequest` (datasets + funciones + grupos), con dos adaptadores equivalentes:
- **CLI**: `--dataset ex/mt/sym/tf` repetible, `--function NAME[:k=v,...]` repetible (gramática compartida de `finances_core`; claves de parámetros o de entradas, p. ej. `-f rsi:timeperiod=14,price=adj_close -f bbands:timeperiod=20,matype=ema`), `--group "Pattern Recognition"` repetible (todas las funciones soportadas del grupo con defaults), o `--request-file FILE.json` con exactamente el cuerpo de la API.
- **API**: el mismo `TaLibComputeRequest` como cuerpo JSON.
- Nombres de función sin distinción de mayúsculas. Límites: 1–100 datasets; 1–200 funciones tras expandir grupos. Duplicados (mismo `series_id`) se deduplican sin error.
- **SI** la función o el grupo no existen, **ENTONCES** exit 2 / HTTP 400 listando los grupos válidos.

### RF-6 — Convenciones de Datos (Ubícuo)
- **Alineación 1:1**: cada salida **DEBE** tener una fila por fila fuente, con el mismo `open_time_ms`.
- **Calentamiento**: los `NaN` de TA-Lib se persisten como nulos. Para salidas enteras (donde TA-Lib rellena con 0), las filas anteriores al *lookback* de la función **DEBEN** persistirse como nulas.
- **Sin anticipación**: el valor en `t` **DEBE** depender solo de filas `≤ t`. Un test de propiedad recorre **todo** el catálogo soportado con defaults (prefijo estable); la función que falle se marca `non_causal` (RF-2).
- **Nulos en entradas**: TA-Lib propaga `NaN`; **EL SISTEMA DEBE** ejecutar cada función sobre la subsecuencia de filas con todas sus entradas válidas y asignar nulo a las filas excluidas (warning con el conteo). Diferencia documentada con `ft_indicators`: aquí las ventanas "saltan" las filas excluidas.
- **Historia insuficiente**: columna toda nula + warning `insufficient_history`; la tarea no falla.
- La configuración global de TA-Lib (*unstable period* = 0, compatibilidad por defecto) **NO DEBE** modificarse en tiempo de ejecución; se registra en el manifiesto.

### RF-7 — Tipos de Salida (Ubícuo)
| Salida de TA-Lib | Tipo persistido | Ejemplos |
|---|---|---|
| Real (`double`) | `Float32` (un único `cast` en la frontera; Art. 5) | `RSI`, `BBANDS`, `HT_DCPERIOD` |
| Entera de **patrón** (grupo *Pattern Recognition*: −200…+200) | `Int16` (RFC-0002) | `CDLENGULFING` (±100), `CDLHIKKAKE` (±100/±200) |
| Entera **categórica** (fuera de patrones) | `Int16` (RFC-0002) | `HT_TRENDMODE` (0/1) |
| Entera de **índice** (posición de una fila) | `Int64` = `open_time_ms` de la fila referida | `MAXINDEX`, `MININDEX`, `MINMAXINDEX` |

- `Int8` **no** es suficiente: algunos patrones devuelven ±200 (fuera de −128…127).
- Los índices crudos de TA-Lib dependen del inicio de la serie; convertirlos a `open_time_ms` los hace estables y unibles.
- **SI** una salida entera supera el rango de `Int16`, **ENTONCES** el item falla con error `output_out_of_range` (no se trunca).
- TA-Lib calcula internamente en `double`; esa frontera `Float64` queda aislada en el motor (RFC-0002).

### RF-8 — Persistencia (Ubícuo)
**EL SISTEMA DEBE** escribir con el almacén compartido (`finances_core.indicators_store`, espacio de nombres `ft_ta_lib`):
`{FT_INDICATORS_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/year={YYYY}/month={MM}/{series_id}.parquet`
- Columnas `open_time_ms` (Int64) + salidas (RF-7); reemplazo atómico por mes, sin pérdida de filas; huérfanos eliminados; series no pedidas intactas.
- Manifiesto `{…}/timeframe={Z}/_manifest.ft_ta_lib.json` (spec resuelta, columnas, dtypes, versión TA-Lib, filas, primer/último `open_time_ms`, `stability`, fecha) y bloqueo `_lock.ft_ta_lib`.
- Todos los `series_id` empiezan por `talib_` (RF-9 de nomenclatura): nunca colisionan con `ft_indicators`, que comparte los directorios.

### RF-9 — Nomenclatura Determinista (Ubícuo)
- **`series_id`** = `talib_{función en minúsculas}[_{parámetros en el orden de TA-Lib}][_{entrada}_{columna} por cada entrada redefinida]`, con todos los parámetros (incluidos default); reales con la regla compartida (`.`→`p`, negativo → `m`); tipos de media por nombre (`sma`, `ema`, …).
- **Columnas**: `{series_id}` si hay una sola salida; `{series_id}_{salida de TA-Lib en minúsculas}` si hay varias.
- Ejemplos: `talib_rsi_14`, `talib_rsi_14_price_adj_close`, `talib_bbands_5_2_2_sma_upperband`, `talib_macd_12_26_9_macdsignal`, `talib_stoch_5_3_sma_3_sma_slowk`, `talib_cdlengulfing`, `talib_cdlmorningstar_0p3`, `talib_minmaxindex_30_maxidx`.

### RF-10 — Idempotencia, Concurrencia y Exportación
- Relanzar la misma petición sobre la misma fuente **DEBE** producir archivos con contenido idéntico (misma versión de TA-Lib).
- **SI** otra tarea no terminada de `ft_ta_lib` está calculando un dataset pedido, **ENTONCES** exit 1 / HTTP 409 (el bloqueo de `ft_indicators` no interfiere).
- `export` (CLI) / `POST /ta-lib/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export`: mismas reglas que `ft_indicators` RF-9 (rango, series/columnas, `include_ohlcv`, `stability` all/flag/drop, formatos `parquet`/`csv`/`jsonl`, stdout o directorio, `stale`, rango vacío exit 3 / 400, no calculado exit 1 / 404) sobre la vista compartida; solo exporta series del espacio `ft_ta_lib`. Las columnas `Int16`/`Int64` se exportan con su tipo.

### RF-11 — Registro de Progreso en SQLTaskManager (Estado-Driven)
- Estados `PENDING` → `PLANNING` → `COMPUTING` → `COMPLETED` / `FAILED` (runner compartido); `task_id` UUID v7 creado al despachar.
- Errores por item (`output_out_of_range`, excepción de TA-Lib en ejecución) y por dataset (`invalid_source`) se acumulan en `errors` y la tarea sigue. `FAILED` solo ante IO o excepción inesperada.

### RF-12 — Interfaces CLI y API Simétricas (Ubícuo)
- **CLI** `ft_ta_lib`: `catalog`, `compute` (con `--dry-run`), `plan`, `status`, `result`, `show`, `export`; flags globales `-v/-q/--log-format`; exit codes 0 OK, 1 no encontrado/bloqueado/genérico, 2 validación, 3 tarea `FAILED` o exportación vacía, 4 IO.
- **API** (prefijo `/ta-lib`, montado por `ft_serve`): `GET /catalog`; `POST /compute` → `202` (`task_id`, `status="ACCEPTED"`, `poll_url`, `plan_url`, `result_url`) en ≤ 200 ms; `GET /status|/plan|/result/{task_id}` (404 / 409); `GET /datasets/{…}` (manifiesto); `POST /datasets/{…}/export`. El cálculo nunca bloquea el event loop (Art. 3).

### RF-13 — Manejo de Errores Específicos (Evento-Driven)
| Situación | Comportamiento |
|---|---|
| Dataset inexistente al despachar | exit 1 / HTTP 404, sin crear tarea |
| Función o grupo desconocido | exit 2 / HTTP 400 con grupos válidos |
| Función no soportada (`unavailable_input`, `non_causal`) | exit 2 / HTTP 400 con el motivo |
| Parámetro inexistente, de tipo erróneo, fuera de rango o `TA_BAD_PARAM` | exit 2 / HTTP 400 con el detalle de TA-Lib |
| Redefinición de entrada inválida / `adj_close` ausente | exit 2 / HTTP 400 |
| Historia menor que el *lookback* | columna toda nula + `insufficient_history` |
| Salida entera fuera de `Int16` | error `output_out_of_range` en el item, la tarea sigue |
| Excepción de TA-Lib durante el cálculo | error en el item, la tarea sigue |
| Dataset bloqueado por otra tarea de `ft_ta_lib` | exit 1 / HTTP 409 |
| Error de IO al escribir | `FAILED` (CLI exit 3), archivos previos intactos |
| `stability=drop` con `stable_from_ms` nulo | exit 2 / HTTP 400 |

### RF-14 — Logging Dual (Art. 9)
`configure_logging(component="ft_ta_lib")`; contexto `task_id`, `symbol`, `timeframe`, `dataset`, `series_id`, `function`, `group`; métricas `compute_duration_ms`, `rows`, `rows_per_s`.

### RF-15 — Desempeño (Art. 20)
Sobre el mismo dataset sintético de 1.000.000 velas `1m` que `ft_indicators`, en `ubuntu-latest`:

| ID | Escenario | Umbral provisional |
|---|---|---|
| B1 | Motor en memoria: las 13 funciones equivalentes al catálogo de `ft_indicators` (defaults) | ≤ 1,0 s |
| B2 | Tarea extremo a extremo de B1 | ≤ 8,0 s |
| B3 | Tarea extremo a extremo del grupo *Pattern Recognition* completo (61 funciones) | ≤ 20,0 s |

Calibración como en `ft_indicators` RF-14: 1,5 × mediana de 5 repeticiones en la primera ejecución real; después, umbral de regresión en CI.

### RF-16 — Configuración (Ubícuo)
**EL SISTEMA DEBE** reutilizar `FT_INDICATORS_DIR` y `FT_INDICATORS_MAX_CONCURRENT` (compartidas con `ft_indicators`), `FT_PARQUET_DIR`, `FT_SHARE_DIR` y `FT_TASKS_DB_PATH`. No añade variables nuevas.

### RF-17 — Metadatos de Estabilidad (Ubícuo)
- Funciones **sin** el *flag* de TA-Lib *unstable period*: `stable_from_ms` = primera fila no nula de cada columna.
- Funciones **con** ese *flag* (EMA, RSI, ATR, ADX, KAMA, MAMA, T3, HT_*, …): `stable_from_ms` nulo con motivo `unstable_period_unknown` (TA-Lib no publica una regla analítica; `ft_indicators` sí la da para sus 12 indicadores).
- Se publica en manifiesto, resultado, progreso y catálogo (contrato `Stability` compartido).

### RF-18 — Validación Cruzada con `ft_indicators` (Ubícuo)
**EL SISTEMA DEBE** acordar con `ft_indicators`, para cada indicador de este último con `talib_equivalent`, dentro de la tabla de tolerancias de `ft_indicators` (plan §3.9) desde su `stable_from_ms` y con sus ajustes documentados. El test vive en `packages/finances-api/tests/` y usa **solo HTTP** sobre `create_app()` (sin importar ninguna herramienta, Art. 4): calcula y exporta el mismo dataset sintético por `/indicators` y `/ta-lib` y compara columnas, tomando el mapeo de `GET /indicators/catalog` (`talib_equivalent`).

---

## Casos Límite y Fuera de Alcance (No-Go)

- **Señales de trading** y combinaciones de funciones (p. ej. RSI de un MACD): fuera del MVP.
- **Funciones con entradas no OHLCV** (`MAVP` con `periods`): catalogadas como no soportadas.
- **Cambiar la configuración global de TA-Lib** (*unstable period*, modo Metastock): fuera del MVP.
- **Recálculo incremental**: igual que `ft_indicators`, fuera del MVP.
- **Reglas analíticas de convergencia** para funciones con *unstable period*: fuera del MVP (motivo `unstable_period_unknown`).
- **Tablas de tolerancia `f32`** y rol de oráculo: corresponden a `ft_indicators`; `ft_ta_lib` no es oráculo.
- **pandas** y la API de *DataFrame* de TA-Lib: se usan arrays NumPy `float64` en la frontera.
