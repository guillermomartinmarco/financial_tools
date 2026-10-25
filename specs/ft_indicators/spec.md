# Spec — `ft_indicators` (MVP v1.0)

## Contexto e Historias de Usuario

Los descargadores (`ft_market_data_binance`, `ft_market_data_yfinance`) dejan OHLCV canónico en Parquet Hive.
El siguiente eslabón de la suite es transformar esas velas en **indicadores técnicos** (columnas numéricas por
vela) que puedan consumir el futuro backtester, notebooks o software externo. `ft_indicators` es la primera
herramienta del nuevo paquete `packages/finances-analytics`: calcula un catálogo cerrado de indicadores con
implementaciones propias en expresiones nativas de **Polars** (sin TA-Lib ni pandas en ejecución; TA-Lib solo como
referencia en los tests de CI), **persiste** los resultados
en Parquet Hive alineados 1:1 con la fuente y los **exporta** a Parquet / CSV / JSON Lines.

`ft_indicators` es además el **oráculo de referencia** de la suite: calcula internamente en `Float64` sobre CPU y
contra él se validarán los futuros kernels GPU en `float32` (spec futura de backtesting; WGSL no tiene `f64`).
Los experimentos WebGPU/WGSL del Tech Lead (`github.com/guillermomartinmarco/-conocimientos`,
`tutorial_gpu_wsgl_mercado`) mostraron que una EMA solo coincide bit a bit tras ~160 velas de convergencia
(~6,6 % de la serie descartada) y que RSI, Bollinger y MACD nunca coinciden bit a bit en `f32`. Por eso la
herramienta publica, además de los nulos de calentamiento, **desde qué vela cada indicador deja de depender de su
semilla** (`stable_from_ms`) y una **tabla de tolerancias** esperadas para implementaciones `f32`.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** calcular RSI, MACD, Bandas de Bollinger, ATR, etc. sobre cualquier dataset descargado (Binance o Yahoo) **para que** pueda analizarlos sin reimplementarlos en cada notebook. |
| US-2 | Backtest Engineer | Como autor del backtester, **exijo** que cada valor del indicador en la vela `t` use solo datos hasta el cierre de `t` y que comparta exactamente el `open_time_ms` de la vela fuente **para que** pueda hacer *joins* exactos sin sesgo de anticipación (*look-ahead*). |
| US-3 | Data Engineer | Como ingeniero de datos, **necesito** que recalcular sea **idempotente** (misma fuente ⇒ mismos archivos) **para que** las ejecuciones programadas sean confiables. |
| US-4 | Analista externo | Como usuario de Excel / R / DuckDB, **quiero** exportar los indicadores (opcionalmente junto con el OHLCV) a CSV, Parquet o JSONL **para que** pueda usarlos fuera de la suite. |
| US-5 | Operador | Como operador, **quiero** `ft_indicators compute` y `POST /indicators/compute` con el mismo contrato de despacho y *polling* que los descargadores **para que** pueda lanzar cálculos desde la terminal o desde `ft_serve`. |

---

## Requisitos Funcionales (EARS)

### RF-1 — Pipeline de Cálculo Determinista (Ubícuo)
**EL SISTEMA DEBE** ejecutar, para cada dataset de la petición:
1. **Validar Petición** (RF-5): indicadores del catálogo (RF-2) y parámetros dentro de rango. **SI** algo es inválido, **ENTONCES** rechazo inmediato (exit 2 / HTTP 400) con el detalle; **SI** un dataset no existe, **ENTONCES** exit 1 / HTTP 404.
2. **Planificar**: resolver defaults, `indicator_id` y columnas de cada (dataset, indicador) y **persistir el plan** en `<raíz del proyecto>/share/plans/indicators_plan_{task_id}.json`.
3. **Cargar** la serie fuente **completa** del dataset (solo `open_time_ms` y las columnas de entrada necesarias) desde `{FT_PARQUET_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/` y validarla (RF-3).
4. **Calcular** todos los indicadores pedidos sobre la serie completa en memoria (**recálculo completo** en cada ejecución).
5. **Reescribir** los archivos de cada indicador pedido (RF-7), actualizar el manifiesto del dataset y **cerrar la tarea** (RF-10).

La fuente es de **solo lectura**: **EL SISTEMA NO DEBE** escribir nunca bajo `FT_PARQUET_DIR`.

### RF-2 — Catálogo de Indicadores del MVP (Ubícuo)
**EL SISTEMA DEBE** ofrecer exactamente este catálogo (nombre `name` en minúsculas). `source` ∈ {`open`, `high`, `low`, `close`, `adj_close`}, default `close` (RF-6). "Calentamiento" = número de filas iniciales con valor nulo sobre una serie sin nulos.

| `name` | Parámetros (default) | Entradas | Columnas de salida (defaults) | Convención / fórmula | Calentamiento |
|---|---|---|---|---|---|
| `sma` | `period`=20, `source` | source | `sma_20` | Media aritmética de las últimas `period` filas | `period−1` |
| `ema` | `period`=20, `source` | source | `ema_20` | α = 2/(period+1); semilla = SMA de las primeras `period` filas; luego `y_t = y_{t−1} + α·(x_t − y_{t−1})` (TA-Lib *classic* / StockCharts) | `period−1` |
| `wma` | `period`=20, `source` | source | `wma_20` | Pesos lineales 1..`period` (el más reciente pesa `period`) / Σpesos | `period−1` |
| `rsi` | `period`=14, `source` | source | `rsi_14` | Wilder (1978): Δ_t = x_t − x_{t−1}; G, L = media simple de las primeras `period` ganancias/pérdidas (semilla en `t=period`), luego suavizado de Wilder α = 1/`period`; RSI = 100·G/(G+L); **SI** G+L = 0 (sin movimiento) **ENTONCES** 0 (compatible con TA-Lib) | `period` |
| `macd` | `fast`=12, `slow`=26, `signal`=9, `source` | source | `macd_12_26_9_line`, `macd_12_26_9_signal`, `macd_12_26_9_hist` | line = EMA(fast) − EMA(slow), cada EMA con su propia semilla SMA (convención Appel / StockCharts; TA-Lib alinea las semillas y difiere en las primeras velas); signal = EMA(`signal`) de line con semilla SMA de los primeros `signal` valores de line; hist = line − signal | line: `slow−1`; signal/hist: `slow+signal−2` |
| `bb` | `period`=20, `std`=2.0, `source` | source | `bb_20_2_middle`, `bb_20_2_upper`, `bb_20_2_lower` | middle = SMA; σ **poblacional** (ddof = 0, como Bollinger y TA-Lib); upper/lower = middle ± `std`·σ | `period−1` |
| `atr` | `period`=14 | high, low, close | `atr_14` | TR_t = max(H−L, \|H−C_{t−1}\|, \|L−C_{t−1}\|) desde `t=1`; ATR semilla = media de TR[1..period] en `t=period`; luego suavizado de Wilder (como TA-Lib) | `period` |
| `stoch` | `k`=14, `smooth_k`=3, `d`=3 | high, low, close | `stoch_14_3_3_k`, `stoch_14_3_3_d` | %K rápido = 100·(C − LL_k)/(HH_k − LL_k) (**SI** HH = LL **ENTONCES** 0, compatible con TA-Lib); %K = SMA(%K rápido, `smooth_k`); %D = SMA(%K, `d`) (*Full Stochastic* de StockCharts; TA-Lib STOCH con medias simples) | %K: `k+smooth_k−2`; %D: `k+smooth_k+d−3` |
| `roc` | `period`=12, `source` | source | `roc_12` | 100·(x_t / x_{t−period} − 1); **SI** x_{t−period} = 0 **ENTONCES** nulo | `period` |
| `obv` | — | close, volume | `obv` | OBV_0 = volume_0 (TA-Lib); OBV_t = OBV_{t−1} ± volume_t según C_t >/< C_{t−1}; igual si C_t = C_{t−1} | 0 |
| `adx` | `period`=14 | high, low, close | `adx_14`, `adx_14_plus_di`, `adx_14_minus_di` | Wilder: +DM/−DM y TR desde `t=1`; sumas suavizadas de Wilder con la **semilla de TA-Lib** (S = Σ de los primeros `period−1` valores y un paso de Wilder en `t=period`: S_period = S·(1 − 1/period) + x_period; luego S_t = S_{t−1} − S_{t−1}/period + x_t; StockCharts/Wilder 1978 siembran con Σ de los primeros `period` y difieren solo hasta `stable_from_ms`); ±DI = 100·S_{±DM}/S_TR (**SI** S_TR = 0 **ENTONCES** 0); DX = 100·\|+DI − −DI\|/(+DI + −DI) (**SI** suma 0 **ENTONCES** 0); ADX semilla = media de los primeros `period` DX; luego Wilder (como TA-Lib) | ±DI: `period`; ADX: `2·period−1` |
| `vwap` | `period`=20 | high, low, close, volume | `vwap_20` | VWAP **móvil**: Σ(TP·V)/ΣV sobre las últimas `period` filas, TP = (H+L+C)/3; **SI** ΣV = 0 **ENTONCES** nulo | `period−1` |

Restricciones de parámetros: enteros en `[2, 10000]` (`roc.period` en `[1, 10000]`); `macd.fast < macd.slow`; `bb.std` en `(0, 10]` con como máximo 2 decimales. **SI** un parámetro viola su restricción o no existe para ese indicador, **ENTONCES** exit 2 / HTTP 400 indicando parámetro, valor y rango válido.

**Decisión VWAP**: el VWAP **anclado a sesión** queda **fuera** del MVP: en cripto 24/7 no existe sesión (cualquier ancla UTC es arbitraria), en acciones requiere calendario y zona horaria de la bolsa que el esquema canónico (solo `open_time_ms` UTC) no transporta, y en timeframes ≥ `1d` carece de sentido. Se incluye el VWAP **móvil** de `period` velas, bien definido para cualquier mercado y timeframe.

**Valores de referencia** (Ubícuo): **EL SISTEMA DEBE** validar en CI cada indicador con equivalente en TA-Lib comparándolo **directamente** contra TA-Lib (`ta-lib`, dependencia de test del extra `[dev]`; ruedas binarias para Windows y manylinux x86_64, cp312), además de contra ejemplos publicados (hojas de StockCharts ChartSchool, ejemplos de Wilder 1978). Equivalencias: `sma`↔`SMA`, `ema`↔`EMA`, `wma`↔`WMA`, `rsi`↔`RSI`, `macd`↔`MACD`, `bb`↔`BBANDS(matype=SMA)`, `atr`↔`ATR`, `stoch`↔`STOCH(slowk/slowd_matype=SMA)`, `roc`↔`ROC`, `obv`↔`OBV`, `adx`↔`ADX`/`PLUS_DI`/`MINUS_DI`; `vwap` móvil no tiene equivalente (solo ejemplos calculados a mano). Diferencias de convención conocidas, tratadas como **ajustes explícitos del test** (no como tolerancia):
- `macd`: TA-Lib alinea las semillas de las EMAs y emite las tres salidas desde el índice `slow+signal−2`; se compara desde `stable_from_ms` (RF-16).
- `stoch`: TA-Lib emite %K y %D desde `k+smooth_k+d−3`; se compara desde ese índice.
- `roc`: con base 0 TA-Lib devuelve 0 y `ft_indicators` nulo; esas filas se excluyen de la comparación.
- Series con nulos/NaN: TA-Lib propaga `NaN`; la comparación usa series sin nulos (la convención de nulos se prueba aparte, RF-3).

### RF-3 — Convenciones Numéricas Comunes (Ubícuo)
- **Sin anticipación**: el valor en la fila `t` **DEBE** depender solo de las filas `≤ t` (calculado al cierre de `t`). Calcular sobre el prefijo `[0..t]` **DEBE** dar el mismo valor en `t` que calcular sobre la serie completa.
- **Alineación 1:1**: cada salida **DEBE** tener exactamente una fila por fila fuente, con el mismo `open_time_ms`; las filas de calentamiento se **conservan con nulo** (no se eliminan).
- **Precisión** (Art. 5/8, excepción justificada del Art. 5.3): los kernels **DEBEN** calcular en `Float64` (aislado en el módulo `kernels/`, rol de oráculo, RF-17) y **DEBEN** convertir a `Float32` en la frontera de persistencia; todo lo persistido o exportado es `Float32` + `open_time_ms` `Int64`. La única salida `Float64` es el paquete de *fixtures* del oráculo (RF-17), que es un artefacto de test, no un camino de datos.
- **Valores no finitos**: `NaN` y `±inf` en las entradas se tratan como nulos.
- **Nulos en entradas** (Estado-Driven): **MIENTRAS** una ventana de un indicador de memoria finita (`sma`, `wma`, `bb`, `stoch`, `roc`, `vwap`) contenga una fila nula, su salida **DEBE** ser nula. En los recursivos (`ema`, `rsi`, `macd`, `atr`, `obv`, `adx`) la fila nula recibe salida nula y se **excluye** de la recursión (el estado se mantiene). El número de filas fuente nulas se reporta en `warnings`.
- **Validación de la fuente** (Art. 13): **SI** faltan columnas requeridas, sus tipos no son `Float32`/`Int64`, u `open_time_ms` no es estrictamente creciente y único, **ENTONCES** el dataset se omite con error `invalid_source` y la tarea continúa.
- **Historia insuficiente**: **SI** el dataset tiene menos filas que el calentamiento, **ENTONCES** la columna se escribe completa con nulos, se añade el warning `insufficient_history` y la tarea **no** falla.

### RF-4 — Nomenclatura Determinista (Ubícuo)
**EL SISTEMA DEBE** derivar de forma determinista y biyectiva:
- **`indicator_id`** = `{name}_{p1}_{p2}…[_{source}]`, con **todos** los parámetros (incluidos los default) en el orden del catálogo; el sufijo `_{source}` solo aparece si `source ≠ close`. Decimales: `.` → `p` sin ceros sobrantes (`std=2.0` → `2`, `2.5` → `2p5`).
- **Columnas**: `{indicator_id}` si la salida es única; `{indicator_id}_{salida}` si es múltiple (`macd_12_26_9_signal`, `bb_20_2p5_upper`, `sma_50_adj_close`, `adx_14_plus_di`).
- Identificadores en `[a-z0-9_]`, seguros como nombre de archivo y de columna SQL.
- La función de nomenclatura vive en `finances_core` (contrato público para el futuro backtester, Art. 4).

### RF-5 — Petición de Indicadores (Ubícuo)
**EL SISTEMA DEBE** aceptar una única forma canónica de petición, `IndicatorComputeRequest` (datasets + lista de `IndicatorSpec{name, params}`), con dos adaptadores equivalentes:
- **CLI (vía principal)**: `--dataset exchange/market_type/symbol/timeframe` repetible y `--indicator name[:k=v,k=v]` repetible (p. ej. `-i rsi -i macd:fast=12,slow=26,signal=9 -i sma:period=50,source=adj_close`). Opcionalmente `--request-file FILE.json` con **exactamente** el cuerpo JSON de la API (conjuntos de indicadores reutilizables y reproducibles); no se combina con `--dataset`/`--indicator`.
- **API**: el mismo `IndicatorComputeRequest` como cuerpo JSON.
- Límites: 1–100 datasets, 1–50 indicadores. Indicadores repetidos (mismo `indicator_id`) se deduplican sin error.
- **SI** `name` no está en el catálogo, **ENTONCES** exit 2 / HTTP 400 listando los nombres válidos.

### RF-6 — Columna de Entrada: `close` vs `adj_close` (Ubícuo)
- El default de `source` **DEBE** ser `close` (precio crudo) en todos los exchanges: es lo que cotizó y lo que operará el backtester; los indicadores OHLC (`atr`, `stoch`, `adx`, `vwap`) no tienen versión ajustada de `high`/`low` y mezclar `adj_close` con H/L crudos sería inconsistente; y `adj_close` de Yahoo cambia retroactivamente con cada dividendo.
- `source=adj_close` es **opt-in** solo para indicadores de una serie (`sma`, `ema`, `wma`, `rsi`, `macd`, `bb`, `roc`) y queda codificado en el `indicator_id` (RF-4).
- **SI** se pide `source=adj_close` sobre un dataset sin esa columna (p. ej. Binance), **ENTONCES** exit 2 / HTTP 400.
- **SI** se pide `source` en un indicador OHLC, **ENTONCES** exit 2 / HTTP 400 (parámetro inexistente).

### RF-7 — Persistencia en Parquet Hive (Ubícuo)
**EL SISTEMA DEBE** escribir cada indicador en su propio archivo, con **las mismas claves Hive que la fuente**:
`{FT_INDICATORS_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/year={YYYY}/month={MM}/{indicator_id}.parquet`
- Columnas: `open_time_ms` (Int64) + columnas de salida (Float32); zstd, estadísticas activadas; filas ordenadas.
- Espacio de nombres: los `indicator_id` de `ft_indicators` nunca empiezan por `talib_` (prefijo reservado a `ft_ta_lib`, que escribe en los mismos directorios mensuales).
- Cada ejecución **reescribe completo** cada indicador pedido: cada archivo mensual se reemplaza de forma atómica (temporal + reemplazo) y **DEBE** contener exactamente las filas fuente de ese mes (alineación 1:1, sin pérdida de filas); los archivos de ese indicador en meses que ya no tienen filas fuente se eliminan. Los indicadores **no** pedidos no se tocan.
- Manifiesto por dataset y herramienta en `{…}/timeframe={Z}/_manifest.ft_indicators.json` (prefijo `_`: ignorado por lectores Hive; el sufijo de herramienta permite que `ft_ta_lib` comparta el directorio sin colisiones) con, por `indicator_id`: spec resuelta, columnas, versión del motor, filas, primer y último `open_time_ms` calculado, `stable_from_ms` (RF-16) por indicador y por columna, fecha de cálculo. Se escribe de forma atómica **después** de las particiones; sirve a `show` y a la detección de desactualización en `export`.
- `FT_INDICATORS_DIR` **NO DEBE** estar dentro de `FT_PARQUET_DIR`. **SI** lo está, **ENTONCES** toda operación falla con error de configuración (CLI exit 2 / API HTTP 500) — evitaría que `ft_data_viewer` y los inventarios lo confundan con un exchange.

### RF-8 — Idempotencia y Concurrencia (Art. 12)
- Relanzar la misma petición sobre la misma fuente **DEBE** producir archivos con contenido idéntico (mismas filas, nulos y valores `Float32`) y el mismo manifiesto salvo la fecha de cálculo; relanzar tras una interrupción **DEBE** converger al mismo resultado que una ejecución sin interrupción.
- **SI** otra tarea no terminada de `ft_indicators` está calculando un dataset pedido (el bloqueo es por dataset y herramienta; `ft_ta_lib` usa el suyo), **ENTONCES** la petición se rechaza (exit 1 / HTTP 409) indicando el `task_id` que lo bloquea. **SI** el bloqueo pertenece a una tarea ya terminada o inexistente (caída), **ENTONCES** se considera obsoleto y se reemplaza.

### RF-9 — Exportación (Ubícuo)
**EL SISTEMA DEBE** proveer `export` (CLI) / `POST /indicators/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export` (API) que, para **un** dataset:
- Filtre por rango temporal (`start`, `end`; `end` exclusivo) y por indicadores (`indicator_id` o `IndicatorSpec`) o columnas concretas; sin filtro = todos los indicadores calculados.
- Con `include_ohlcv` añada las columnas OHLCV fuente (`open`…`volume` y `adj_close` si existe) unidas por `open_time_ms`.
- Produzca una tabla ancha: `exchange`, `market_type`, `symbol`, `timeframe`, `open_time_ms` (int64 ms UTC), [OHLCV], columnas de indicadores; ordenada por `open_time_ms`.
- Opción `stability` (RF-16): `all` (default, todas las filas), `flag` (añade una columna booleana `{indicator_id}_is_stable` por indicador exportado, `true` desde su `stable_from_ms`) o `drop` (elimina las filas anteriores al mayor `stable_from_ms` de los indicadores exportados). **SI** algún indicador exportado tiene `stable_from_ms` nulo y se pide `drop`, **ENTONCES** exit 2 / HTTP 400 indicando cuál.
- Formatos: `parquet` (default), `csv`, `jsonl`; destino `--output-dir DIR` (archivo `{exchange}_{market_type}_{symbol}_{timeframe}_indicators.{ext}`) o stdout (`-`), en streaming.
- **SI** un indicador pedido no está calculado, **ENTONCES** exit 1 / HTTP 404 sugiriendo `compute`. **SI** el rango no tiene filas, **ENTONCES** exit 3 / HTTP 400 (misma convención que `ft_data_viewer` RF-6). **SI** la fuente tiene velas posteriores al último `open_time_ms` calculado de algún indicador, **ENTONCES** se exporta lo persistido y se emite un warning (CLI: stderr; API: cabecera `X-FT-Indicators-Stale`).
- **SI** `output_dir` está dentro de `FT_PARQUET_DIR` o `FT_INDICATORS_DIR`, **ENTONCES** exit 2 / HTTP 400.
- La exportación no recalcula: solo lee lo persistido.

### RF-10 — Registro de Progreso en SQLTaskManager (Estado-Driven)
- Estados: `PENDING` → `PLANNING` → `COMPUTING` → `COMPLETED` / `FAILED`; las fases solo avanzan. La fila se crea al despachar con `task_id` UUID v7.
- Progreso: `datasets_total/done`, `items_total/done` (pares dataset×indicador), `rows_computed`, `partitions_written`, `warnings`, `errors`.
- Errores por dataset (`invalid_source`, dataset desaparecido tras el despacho) se acumulan en `errors` y la tarea **sigue**.
- `FAILED` **solo** ante error de IO al escribir (disco, permisos) o excepción inesperada; cada archivo queda en su versión anterior o en la nueva, nunca a medias (escritura atómica), y una re-ejecución lo corrige (RF-8).

### RF-11 — Interfaces CLI y API Simétricas (Ubícuo)
- **CLI** `ft_indicators`: `catalog`, `compute` (con `--dry-run`), `plan`, `status`, `result`, `show`, `export`. `catalog`/`plan`/`status`/`result`/`show` imprimen el mismo JSON que la API. Flags globales `-v/-q/--log-format`. `compute` corre en proceso registrando la tarea en `tasks.db`. Exit codes: 0 OK, 1 no encontrado/bloqueado/genérico, 2 validación, 3 tarea `FAILED` o exportación vacía, 4 error de IO.
- **API** (prefijo `/indicators`): `GET /catalog`; `POST /compute` → `202` con `task_id`, `status="ACCEPTED"`, `poll_url`, `plan_url`, `result_url` en ≤ 200 ms (Art. 18); `GET /status/{task_id}`, `GET /plan/{task_id}`, `GET /result/{task_id}` (`404` inexistente, `409` aún no terminado); `GET /datasets/{exchange}/{market_type}/{symbol}/{timeframe}` (manifiesto, `404`); `POST /datasets/{…}/export`. Cálculo y exportación nunca bloquean el event loop (Art. 3).

### RF-12 — Manejo de Errores Específicos (Evento-Driven)
| Situación | Comportamiento |
|---|---|
| Dataset inexistente al despachar | exit 1 / HTTP 404, sin crear tarea |
| Dataset desaparece o es ilegible durante la tarea | error en `errors`, se omite, la tarea sigue |
| Indicador desconocido / parámetro inválido o inexistente | exit 2 / HTTP 400 con nombres o rangos válidos |
| `source=adj_close` sin esa columna | exit 2 / HTTP 400 |
| Historia menor que el calentamiento | columna toda nula + warning `insufficient_history` |
| `NaN`/`inf`/nulos en la fuente | tratados como nulos (RF-3) + warning con el conteo |
| Esquema fuente no canónico / timestamps no monótonos | error `invalid_source`, dataset omitido |
| Dataset bloqueado por otra tarea | exit 1 / HTTP 409 |
| Error de IO al escribir | tarea `FAILED` (CLI exit 3), archivos previos intactos |
| Destino de exportación no escribible | exit 4 / HTTP 500 |
| `stability=drop` con algún `stable_from_ms` nulo (`obv`, `not_converged`) | exit 2 / HTTP 400 |

### RF-13 — Logging Dual (Art. 9)
**EL SISTEMA DEBE** configurar el logging con `finances_core.logging.configure_logging(component="ft_indicators")`: Rich en CLI (con barra de progreso por dataset), JSON en servicio, con contexto `task_id`, `symbol`, `timeframe`, `dataset`, `indicator_id` y métricas `compute_duration_ms`, `rows`, `rows_per_s`.

### RF-14 — Desempeño (Art. 20, dimensión *Desempeño*)
**EL SISTEMA DEBE** medir, sobre un dataset sintético determinista de **1.000.000** velas `1m` y los **12** indicadores del catálogo con parámetros default (19 columnas), en un runner `ubuntu-latest` de GitHub Actions:

| ID | Escenario | Umbral calibrado (provisional original) |
|---|---|---|
| B1 | Motor en memoria (sin IO) | ≤ 0,64 s (2,0 s) |
| B2 | Tarea extremo a extremo (leer Parquet + calcular + reescribir particiones y manifiesto) | ≤ 2,51 s (8,0 s) |

Calibrados el 2026-10-25 con la ejecución de `ubuntu-latest` del commit `86d23a2` (medianas B1 0,427 s y B2 1,673 s). Regla de calibración: con la primera ejecución real en `ubuntu-latest` (umbral = 1,5 × la mediana de 5 repeticiones, sin superar el provisional salvo decisión explícita del Tech Lead) y desde entonces un benchmark de regresión en CI **DEBE** fallar si se supera cualquiera.

### RF-15 — Configuración vía `FT_` Settings (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings` (prefijo `FT_`, rutas resueltas desde la raíz del proyecto): `FT_INDICATORS_DIR` (default `./data/indicators`) y `FT_INDICATORS_MAX_CONCURRENT` (datasets en paralelo por tarea, default 2), además de `FT_PARQUET_DIR`, `FT_SHARE_DIR` y `FT_TASKS_DB_PATH`. Las nuevas variables se registran en `env_list.conf` con `source_tool="ft_indicators"`.

### RF-16 — Metadatos de Convergencia de la Semilla (Ubícuo)
**EL SISTEMA DEBE** calcular y publicar, para cada indicador calculado y cada columna de salida, `stable_from_ms`: el `open_time_ms` de la primera vela cuyo valor ya no depende materialmente de la semilla inicial.
- **Regla** (analítica, sin mirar los datos): una etapa de suavizado exponencial con factor α (EMA: α = 2/(n+1); Wilder: α = 1/n) sembrada en el índice `s` deja a la semilla un peso `(1−α)^k` tras `k` pasos; `k* = ⌈ln ε / ln(1−α)⌉` con **ε = 1e-6** (constante versionada con el motor). Índice estable de la etapa = `max(s, índice estable de su entrada) + k*`; una columna derivada de varias etapas toma el máximo.
- Valores de referencia con parámetros default: `ema_20` → índice 158 (k* = 139); `rsi_14` y `atr_14` → 201 (k* = 187); `adx_14_plus_di`/`minus_di` → 200, `adx_14` → 387 (semilla de las sumas en `period−1`); `macd_12_26_9_line` → 205, `signal`/`hist` → 267.
- **Indicadores no recursivos** (`sma`, `wma`, `bb`, `stoch`, `roc`, `vwap`): `stable_from_ms` = primera fila no nula.
- **SI** la semilla nunca decae (`obv`: el nivel depende para siempre del primer volumen), **ENTONCES** `stable_from_ms` es nulo con motivo `non_decaying_seed`.
- Los índices cuentan solo filas no nulas de la entrada (las nulas se excluyen de la recursión, RF-3) y se traducen al `open_time_ms` de esa fila.
- **SI** la serie es más corta que el índice estable, **ENTONCES** `stable_from_ms` es nulo, se añade el warning `not_converged` y la tarea **no** falla.
- `stable_from_ms` **DEBE** figurar en el manifiesto, en `ComputeItemResult`, en el progreso de la tarea y en `GET /catalog` (como fórmula legible).

### RF-17 — Oráculo de Referencia para Implementaciones `f32` (Ubícuo)
- **EL SISTEMA DEBE** publicar en el catálogo, por indicador y columna, la **tolerancia esperada** para una implementación `float32` (p. ej. GPU) comparada con el oráculo, aplicada solo desde `stable_from_ms`; la coincidencia bit a bit **no** se exige para indicadores con divisiones, raíces o cancelaciones (RSI, Bollinger, MACD, Estocástico, ADX).
- **EL SISTEMA DEBE** coincidir de forma **exacta** con cualquier implementación conforme en: filas y `open_time_ms`, máscara de nulos (calentamiento) y resultados de las convenciones de división por cero.
- **EL SISTEMA DEBE** poder generar un **paquete de *fixtures* del oráculo** versionado (series de entrada + salidas esperadas en `Float64` + `stable_from` + tolerancias) en un formato estable e independiente de Python, reutilizable por una futura batería de tests GPU.

---

## Casos Límite y Fuera de Alcance (No-Go)

- **Recálculo incremental** (procesar solo la cola nueva + calentamiento): fuera del MVP. Mejora futura documentada: reanudar los recursivos desde un estado persistido, calcular los de memoria finita por bloques alineados para garantizar equivalencia bit a bit con el recálculo completo, detectar cambios retroactivos de la fuente con huellas de contenido por mes y añadir un test de propiedad *incremental ≡ completo*. El MVP siempre recalcula la serie completa (barato en memoria, RF-14) y su idempotencia es por construcción.
- **Señales de trading** (cruces, umbrales, divergencias, reglas de entrada/salida): fuera del MVP; corresponden a una herramienta futura o al backtester.
- **VWAP anclado a sesión** y cualquier indicador que requiera calendario de mercado (RF-2).
- **TA-Lib, pandas, pandas-ta** como motor o dependencia de ejecución de `ft_indicators` (TA-Lib solo como referencia en tests; el catálogo TA-Lib completo lo cubre la herramienta hermana `ft_ta_lib`, `specs/ft_ta_lib/`).
- **Indicadores definidos por el usuario** (fórmulas libres, plugins): fuera del MVP; el catálogo es cerrado y versionado.
- **Multi-timeframe** (p. ej. RSI diario sobre velas `1h`) y *resampling*: fuera del MVP.
- **Cálculo sobre un subrango temporal**: el cálculo siempre cubre todo el dataset (necesario para la exactitud de los recursivos); el rango solo filtra la exportación.
- **Ajuste por splits/dividendos** al vuelo: el usuario elige `source=adj_close` (RF-6); `high`/`low` ajustados no existen en el esquema.
- **Exportación multi-dataset** en una sola llamada: una llamada por dataset.
