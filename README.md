# Financial Tools Suite (`financial_tools`)

Suite corporativa de utilidades modulares financieras para el procesamiento de datos de mercado, análisis matemático de estrategias, optimización masiva en GPU y trading automatizado.

El proyecto está diseñado bajo la metodología **Spec-Driven Development (SDD)**, garantizando robustez mediante una estricta dualidad operativa: comandos efímeros en consola (CLI) y un demonio continuo de red (API HTTP asíncrona).

---

## 1. Arquitectura del Monorepo

Para garantizar el aislamiento y permitir ciclos SemVer independientes, las herramientas de la suite se agrupan en paquetes distribuidos de la siguiente manera (los marcados *(planificado)* aún no existen):

| Paquete Corporativo | Utilidades Asociadas (`ft_*`) | Stack Tecnológico Base |
| :--- | :--- | :--- |
| **`finances-core`** | `finances_core` (puente compartido: config, logging, tasks, schemas), `ft_setup`, `ft_strategy_manager` (CRUD versionado de estrategias) | Pydantic Settings, Structlog, SQLite |
| **`finances-downloader`** | `ft_market_data_binance`, `ft_market_data_yfinance`, `ft_data_viewer`, `ft_datasets` (recetas de datasets reales reproducibles), `ft_market_data_manager` *(`compact`, `dedupe`, `prune` e `import` implementados)* | Asyncio, Polars, Parquet |
| **`finances-analytics`** | `ft_indicators` (indicadores técnicos sobre OHLCV Hive), `ft_ta_lib` (catálogo completo de TA-Lib sobre OHLCV Hive) | Polars (cálculo `Float64`, persistencia `Float32`), TA-Lib (rueda binaria), Parquet Hive |
| **`finances-api`** | `ft_serve` (raíz de composición: monta los routers de todas las herramientas) | FastAPI, Uvicorn |
| **`finances-backtester`** *(ft_vectorbt operativo; resto planificado)* | `ft_vectorbt`, `ft_backtesting_py`, `ft_backtrader`, `ft_bt`, `ft_ffn`, `ft_backtestopt_spectre` (C++/CUDA), `ft_bot_paper_binance`, `ft_live_bot` | VectorBT, Backtrader, PyBind11, CMake |

---

## 2. Mapa Detallado de Utilidades

### 2.1 Configuración y Gobernanza
*   **`ft_setup`**: Inicialización del entorno, diagnóstico de variables y enmascaramiento de llaves criptográficas/secretos usando emojis de advertencia (`⚠️`).
*   **`ft_strategy_manager`**: CRUD y persistencia formal de las reglas de parámetros de estrategias del usuario: versiones inmutables con hash SHA-256 del JSON canónico, JSON Schema opcional, JSON Patch, diff entre versiones, soft/hard delete y export JSON/YAML. Persiste en `share/strategies.db` (SQLite WAL; `FT_STRATEGIES_DB_PATH` la sobrescribe). Exposición simétrica CLI (`ft_strategy_manager`) y API (`/strategies`).

### 2.2 Captura y Gestión de Datos de Mercado
*   **`ft_market_data_binance`**: Descargador de alta velocidad asíncrono para la API de Binance (Spot/Futures).
*   **`ft_market_data_yfinance`**: Descargador incremental y sin pérdida de OHLCV de Yahoo Finance (acciones, ETFs, índices, forex, futuros y cripto contra fiat; timeframes `1m`, `5m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M`). Respeta los límites de historial intradía de Yahoo (`1m` 30 días, `5m`–`30m` 60 días, `1h` 730 días; lo que queda fuera se informa en `gaps_unfillable`), no trata los cierres de mercado como huecos y detecta el `market_type` (`equity`, `etf`, `index`, `fx`, `crypto`, `future`, `fund`, `other`) a partir de Yahoo. Guarda el OHLC tal como lo entrega Yahoo (**ajustado por splits, no por dividendos**) más `adj_close` en `yfinance/market_type=…/symbol=…/timeframe=…/year=…/month=…/data.parquet`, y los dividendos/splits/capital gains en `yfinance_events/symbol=…/events.parquet` (permite reconstruir el precio cotizado y el retorno total). No requiere API key.
*   **`ft_market_data_manager`**: Gestor del almacén Parquet Hive: compacta, deduplica y borra particiones (con `--dry-run` y verificación de cambios concurrentes), e importa datos corregidos desde Parquet, CSV o JSON Lines a un dataset destino explícito (valida timestamp y alineación, y nunca modifica los timestamps existentes). Especificado en `specs/ft_market_data_manager/`: `compact`, `dedupe`, `prune` e `import` están implementados (CLI `ft_market_data_manager` y API `/market-data/manager`).
*   **`ft_data_viewer`**: Explorador y diagnóstico de datasets OHLCV (listado, estadísticas, gaps, anomalías, exportación, comparación).
*   **`ft_datasets`**: Recetas de **datasets reales reproducibles** para probar masivamente con datos reales. Una receta versionada (`datasets/<nombre>.toml`) fija exchange, `market_type`, símbolos, timeframes y rangos **cerrados y pasados** (`end` exclusivo y al menos `FT_DATASETS_SETTLE_DAYS` días atrás; en Yahoo solo `1d`/`1w`/`1M`); su *lock* (`datasets/<nombre>.lock.json`) fija el hash de contenido canónico `ftds-v1` de cada ítem (bits exactos de `float32`/`int64`: independiente del escritor Parquet, del SO y de la CPU). `materialize` invoca a los descargadores **por su CLI pública como subprocesos** (nunca los importa) sobre el almacén dedicado `FT_DATASETS_DIR` y omite sin red los ítems que ya verifican; `verify` (local, sin red) distingue `OK`, `MISSING`, `MISMATCH` (deriva `ROWS_CHANGED` / `SCALE_RESTATED` con factor / `VALUES_CHANGED`, columnas y meses afectados) y `UNLOCKED`; `lock` escribe el *lock* solo de forma explícita (`--update` para re-bloquear tras una re-expresión del proveedor, en un PR revisado). Receta inicial: `benchmark-v1` (BTCUSDT/ETHUSDT `1m`+`1h` Q1 2024 y AAPL, ^GSPC, EURUSD=X `1d` 2015-2024; ~274 k filas, ~9 MB).

### 2.3 Motores de Backtesting y Análisis
*   **`ft_indicators`** (paquete `finances-analytics`): Catálogo cerrado de 12 indicadores técnicos (`sma`, `ema`, `wma`, `rsi`, `macd`, `bb`, `atr`, `stoch`, `roc`, `obv`, `adx` y `vwap` móvil) implementados con expresiones nativas de Polars (sin TA-Lib ni pandas en ejecución; TA-Lib solo como contraste en los tests de CI). Recalcula la serie completa de cada dataset descargado (Binance o Yahoo) sin anticipación y alineado 1:1 con la fuente (mismo `open_time_ms`, calentamiento conservado como nulo), y persiste cada indicador de forma idempotente en `FT_INDICATORS_DIR/{exchange}/market_type=…/symbol=…/timeframe=…/year=…/month=…/{indicator_id}.parquet` (`Float32`). Publica `stable_from_ms` (desde qué vela el indicador deja de depender de su semilla) y tolerancias `f32` como oráculo de futuros kernels GPU. Exporta a Parquet, CSV o JSON Lines, opcionalmente junto con el OHLCV fuente.
*   **`ft_ta_lib`** (paquete `finances-analytics`): Envoltura delgada sobre **todo** el catálogo de TA-Lib (ta-lib 0.8.3 / TA-Lib C 0.8.3 (fijada con `ta-lib>=0.8.3,<0.8.4`): 237 funciones en 10 grupos, 236 soportadas; `MAVP` no, por requerir la entrada `periods`), descubierto en tiempo de ejecución con `talib.abstract`. Mismo contrato de datos que `ft_indicators` (alineación 1:1 por `open_time_ms`, calentamiento nulo, sin anticipación, recálculo completo idempotente) y mismo `FT_INDICATORS_DIR`, con prefijo `talib_` y manifiesto/bloqueo propios (`_manifest.ft_ta_lib.json`, `_lock.ft_ta_lib`). Salidas reales en `Float32`, patrones de velas y categóricas en `Int16` (±200) e índices (`MAXINDEX`, …) como `open_time_ms` `Int64` (RFC-0002). Nombres deterministas con todos los parámetros (`talib_rsi_14`, `talib_bbands_20_2_2_sma_upperband`, `talib_cdlengulfing`); las funciones con *unstable period* de TA-Lib publican `stable_from_ms` nulo (`unstable_period_unknown`). Exporta igual que `ft_indicators` y se valida contra ella en CI.
*   **`ft_vectorbt`** (paquete `finances-backtester`): Primer motor de backtesting, **operativo**. Simula con [VectorBT](https://vectorbt.dev) (fijado `>=1.2.0,<1.3`) las señales que calcula el núcleo (`finances_core.backtest`) sobre un dataset del almacén Parquet, y persiste operaciones, curva de capital y métricas en `FT_RESULTS_DIR`. El motor **solo simula**: recibe `entries`/`exits` ya calculadas y no conoce `kind` ni parámetros (cualquier estrategia registrada funciona sin tocarlo). La frontera numérica de doble precisión (`pandas`/NumPy/`vectorbt`) queda **confinada a `engine.py`** por [RFC-0004](docs/rfc/); el resto de la suite mantiene `Float32`/`Int64`. Estrategias integradas: `sma_cross` (medias rápida/lenta), `rsi_reversion` (RSI de Wilder) y `donchian_breakout` (canales). Expone CLI (`ft_vectorbt`) y API (`/backtest/vectorbt`) simétricas (Art. 1), con despacho asíncrono 202 + polling y resultados deterministas e idempotentes (misma petición ⇒ archivos byte-idénticos; repetición `cached`).
*   **`ft_backtesting_py` / `ft_backtrader` / `ft_bt`** *(planificados)*: Envolturas modulares que expondrán otros frameworks tradicionales al mismo formato de datos canónico y contrato de motor que `ft_vectorbt`.
    *   **Estrategias propias**: las estrategias viven en el núcleo (`finances_core/backtest/strategies/`, una carpeta por estrategia) y se pueden añadir estrategias Python locales en `FT_STRATEGIES_DIR`; la API nunca carga código. Guía paso a paso: [`docs/guides/strategy-authoring.md`](docs/guides/strategy-authoring.md).
    *   **Licencia de VectorBT**: `vectorbt` (fijado en `>=1.2.0,<1.3`) se distribuye bajo Apache 2.0 con *Commons Clause* v1.0, que **prohíbe venderlo**, incluido ofrecer a terceros por un pago un producto o servicio cuyo valor dependa sustancialmente de él. `ft_vectorbt` es para uso no comercial; un uso comercial exige otro motor (las estrategias viven en el núcleo y no dependen del motor).
*   **`ft_ffn`**: Generador estadístico de métricas avanzadas de rendimiento (Sharpe, Sortino, Drawdown masivo).
*   **`ft_backtestopt_spectre`**: El motor híbrido de alto rendimiento. Código nativo C++/CUDA compilado vía CMake y enlazado mediante PyBind11 para optimizaciones masivas en GPU.

### 2.4 Servidores y Ejecución en Vivo
*   **`ft_bot_paper_binance` / `ft_live_bot`**: Bots de ejecución de órdenes (Simulado / Real) controlados en subprocesos.
*   **`ft_serve`** (paquete `finances-api`): Servidor HTTP central unificado. Monta en una sola app FastAPI los routers de todas las herramientas; las tareas pesadas se despachan sin bloquear el event loop y su estado se persiste con `SQLTaskManager` en `share/tasks.db`.

---

## 3. Guía de Inicio Rápido

> Los paquetes se instalan en modo editable con `pip` (no se usa workspace `uv`).

### Instalación en modo editable
```bash
python -m pip install -e packages/finances-core
python -m pip install -e packages/finances-downloader
python -m pip install -e packages/finances-analytics    # ft_indicators y ft_ta_lib (ta-lib es dependencia de runtime: rueda binaria)
python -m pip install -e "packages/finances-api[service]"   # servicio HTTP (ft_serve, incluye uvicorn)
```

### Ejecución en Modo Efímero (CLI)
```bash
# Inicialización del entorno (directorios, .env.template, .ft_root, tasks.db WAL)
ft_setup init
ft_setup status <task_id>          # estado de una inicialización

# Diagnóstico de variables FT_* (tabla Rich o JSON)
ft_setup doctor
ft_setup doctor --format json

# Descarga de datos históricos Binance (Public Data + REST fallback)
ft_market_data_binance download --symbol BTCUSDT --timeframe 1h --start 2024-01-01
ft_market_data_binance status <task_id>    # progreso (mismo JSON que GET /status)
ft_market_data_binance result <task_id>    # resultado final (mismo JSON que GET /result)

# Descarga de datos históricos Yahoo Finance (símbolos tal cual Yahoo; entrecomillar ^ y =)
ft_market_data_yfinance download -s AAPL -t 1d --start 2024-01-01
ft_market_data_yfinance download -s "^GSPC" -t 1d --start 2024-01-01
ft_market_data_yfinance download -s "EURUSD=X" -t 1h --start 2026-09-01   # 1h: últimos 730 días
ft_market_data_yfinance download -s AAPL -s MSFT -t 1d -t 1w --start 2020-01-01 --dry-run  # solo el plan
ft_market_data_yfinance status <task_id>   # progreso (mismo JSON que GET /status)
ft_market_data_yfinance plan <task_id>     # plan guardado (mismo JSON que GET /plan)
ft_market_data_yfinance result <task_id>   # resultado final (mismo JSON que GET /result)

# Exploración y diagnóstico de datasets descargados
ft_data_viewer list                                    # Lista todos los datasets
ft_data_viewer summary -e binance -m spot -s BTCUSDT -t 1h  # Estadísticas
ft_data_viewer head -e binance -m spot -s BTCUSDT -t 1h -n 20  # Primeras 20 filas
ft_data_viewer gaps -e binance -m spot -s BTCUSDT -t 1h      # Gaps temporales
ft_data_viewer anomalies -e binance -m spot -s BTCUSDT -t 1h  # Duplicados, nulos, outliers
ft_data_viewer candles -e binance -m spot -s BTCUSDT -t 1h --start-ms 1704067200000 --limit 500 --cols open_time_ms,close -f json  # Velas por rango (paginadas: usa next_after_ms como --after-ms)
# API equivalente: GET /data/datasets/binance/spot/BTCUSDT/1h/candles?start_ms=1704067200000&limit=500&columns=open_time_ms,close&after_ms=<next_after_ms>
ft_data_viewer export -e binance -m spot -s BTCUSDT -t 1h --start 2024-01-01 --end 2024-06-01 -o ./subset  # Exportar
ft_data_viewer summary -e yfinance -m equity -s AAPL -t 1d  # Dataset de Yahoo (market_type detectado)
ft_data_viewer compare --exchange-a binance --market-a spot --symbol-a BTCUSDT --timeframe-a 1h \
  --exchange-b binance --market-b futures --symbol-b BTCUSDT --timeframe-b 1h  # Spot vs Futures

# Mantenimiento del almacén Parquet (único que escribe; sobre FT_PARQUET_DIR). Selectores repetibles: -e/-m/-s/-t, --start-ms/--end-ms
# Exit: 0 OK (incluye --dry-run y particiones en conflict) · 1 no encontrado/confirmación rechazada · 2 argumentos · 3 conflicto · 4 IO
ft_market_data_manager compact -e binance -m spot -s BTCUSDT -t 1h --dry-run   # plan: un data.parquet ordenado por mes, sin escribir
ft_market_data_manager compact -e binance -m spot -s BTCUSDT -t 1h -f json     # ejecuta; --format table|json
ft_market_data_manager dedupe -e binance -s BTCUSDT --dry-run                  # duplicados por open_time_ms (prevalece la última fila)
ft_market_data_manager prune -e binance -s BTCUSDT --before-ms 1706745600000 --dry-run  # meses completos anteriores a T; nunca recorta filas
ft_market_data_manager prune -e binance -s BTCUSDT --before-ms 1706745600000   # pide confirmación (rechazarla = exit 1); --force la omite
ft_market_data_manager import --source corregidas.csv -e binance -m spot -s BTCUSDT -t 1h --dry-run  # valida y muestra la vista previa; no escribe
ft_market_data_manager import --source corregidas.parquet -e binance -m spot -s BTCUSDT -t 1h        # vista previa + confirmación (rechazarla = exit 1)
ft_market_data_manager import --source velas.csv --ts-column time --ts-unit s -e binance -m spot -s ETHUSDT -t 1h --create --force  # crea el dataset; unidad explícita
ft_market_data_manager import --source fix.jsonl -e binance -m spot -s BTCUSDT -t 1h --on-conflict replace --dry-run  # skip (defecto) | fail | replace (aborta si close diverge > 1 %, salvo --force)
# import: el destino es siempre explícito; el timestamp (open_time_ms en ms UTC) nunca se adivina, debe caer en la grilla del timeframe y no se redondea;
# las filas existentes nunca cambian de open_time_ms. El formato del archivo sale de --format o de la extensión; la salida es `-f json` (global) o `-o json`.
# API equivalente (202 + poll_url): POST /market-data/manager/{compact,dedupe,prune} {"exchange":["binance"],"dry_run":true},
# POST /market-data/manager/import {"source":"/ruta/fix.csv","exchange":"binance","market_type":"spot","symbol":"BTCUSDT","timeframe":"1h","dry_run":true};
# prune e import exigen "confirm":true o "dry_run":true (400); un archivo inválido deja la tarea en FAILED y /result responde 422 (409 por conflicto); GET /market-data/manager/status/<task_id> y /result/<task_id> (409 si no terminó)

# Datasets reales reproducibles (recetas en datasets/, almacén en FT_DATASETS_DIR = data/datasets)
# Exit: 0 OK · 1 no encontrada · 2 validación · 3 descarga FAILED · 4 IO · 5 deriva / lock rechazado · 6 sin bloquear
ft_datasets list                                   # recetas y estado del lock (sin verificar datos)
ft_datasets show benchmark-v1                      # 7 ítems con su clave + lock (JSON)
ft_datasets materialize benchmark-v1 --dry-run     # plan: ítems omitidos y comandos exactos de los descargadores
ft_datasets materialize benchmark-v1               # descarga lo que falta y verifica (nunca escribe el lock)
ft_datasets verify benchmark-v1                    # VerifyReport JSON (local, sin red); --format markdown = tabla
ft_datasets lock benchmark-v1                      # UNCHANGED si coincide; REJECTED (exit 5) si difiere salvo --update
ft_datasets materialize benchmark-v1 --refetch     # re-descarga solo los ítems en MISMATCH (borra sus meses en FT_DATASETS_DIR)
ft_datasets status <task_id>                       # también plan/result <task_id> (mismo JSON que la API)
FT_PARQUET_DIR=data/datasets ft_data_viewer list   # el almacén de recetas es un árbol Hive normal

# Indicadores técnicos sobre datasets ya descargados (dataset = exchange/market_type/symbol/timeframe)
ft_indicators catalog                                           # catálogo JSON (parámetros, calentamiento, stable_from, tolerancias f32)
ft_indicators compute -d binance/spot/BTCUSDT/1h -i rsi -i macd  # imprime el Task ID y las series calculadas
ft_indicators compute -d yfinance/equity/AAPL/1d -i sma:period=50,source=adj_close -i bb:std=2.5 --dry-run  # solo el plan
ft_indicators status <task_id>                                  # progreso (mismo JSON que GET /indicators/status)
ft_indicators result <task_id>                                  # resultado final (mismo JSON que GET /indicators/result)
ft_indicators show binance/spot/BTCUSDT/1h                      # manifiesto: indicadores calculados y stable_from_ms
ft_indicators export binance/spot/BTCUSDT/1h --include-ohlcv --format csv -o .temp/indicators  # tabla ancha a CSV
ft_indicators export binance/spot/BTCUSDT/1h -i rsi --stability drop --format jsonl -o -       # solo velas estables, a stdout

# Catálogo completo de TA-Lib (mismo FT_INDICATORS_DIR, series con prefijo talib_)
ft_ta_lib catalog --group "Pattern Recognition"                 # catálogo JSON (grupos, parámetros, salidas, lookback)
ft_ta_lib catalog --function bbands                             # una función: parámetros (matype por nombre) y salidas
ft_ta_lib compute -d binance/spot/BTCUSDT/1h -g "Pattern Recognition" -f kama -f ht_trendline  # 63 series
ft_ta_lib compute -d yfinance/equity/AAPL/1d -f rsi:timeperiod=21,price=adj_close -f bbands:matype=ema --dry-run  # solo el plan
ft_ta_lib status <task_id>                                      # progreso (mismo JSON que GET /ta-lib/status)
ft_ta_lib show binance/spot/BTCUSDT/1h                          # manifiesto: series talib_* calculadas, dtypes y versión de TA-Lib
ft_ta_lib export binance/spot/BTCUSDT/1h -f kama -f cdlengulfing --include-ohlcv --format csv -o .temp/ta_lib  # tabla ancha a CSV

# Estrategias versionadas (share/strategies.db; ids = UUID v7; --format table|json|yaml)
# Exit: 0 OK · 1 no encontrada · 2 validación · 3 nombre duplicado · 4 IO
echo '{"fast": 12, "slow": 26, "api_key": "no-se-loguea"}' > params.json
ft_strategy_manager create mean_reversion --params params.json --desc "demo" -t fx -t demo  # crea v1 (imprime strategy_id y hash)
ft_strategy_manager list --tag fx --name mean                        # filtros tag/name/--include-deleted, --limit/--offset
ft_strategy_manager get <strategy_id>                                # última versión; --version 1, --format json|yaml
echo '[{"op": "replace", "path": "/fast", "value": 8}]' > patch.json
ft_strategy_manager update <strategy_id> --patch patch.json          # nueva versión (JSON Patch RFC 6902); o --params completo
ft_strategy_manager update <strategy_id> --desc "nueva" --tag fx     # solo metadatos: también crea versión (snapshot de description/tags)
ft_strategy_manager diff <strategy_id> --from v1 --to v2             # added/removed/changed + metadatos
ft_strategy_manager versions <strategy_id>                           # historial inmutable
ft_strategy_manager export <strategy_id> --format yaml -o strategy.yaml  # estrategia + todas las versiones
ft_strategy_manager delete <strategy_id>                             # soft-delete (idempotente); --purge pide confirmación (--force la omite)

# Backtesting con VectorBT (ft_vectorbt): simula las señales del núcleo sobre un dataset del almacén (dataset = exchange/market_type/symbol/timeframe)
# Exit: 0 OK (incluye cached) · 1 no encontrado (estrategia/dataset/corrida/tarea) · 2 validación · 3 tarea FAILED / runtime de estrategia · 4 IO
ft_vectorbt kinds                                                    # estrategias registradas (integradas + locales); --kind X filtra; --check corre el contrato de autor (solo CLI)
ft_vectorbt run -d binance/futures/BTCUSDT/1h --strategy-id <id>     # síncrono; repetir devuelve cached; --force recalcula (archivos byte-idénticos); --start-ms/--end-ms/--initial-cash/--size-fraction/--fees-bps/--slippage-bps
ft_vectorbt run -d binance/futures/BTCUSDT/1h --request-file req.json --format json  # req.json = cuerpo exacto de POST /backtest/vectorbt/run
ft_vectorbt show <run_id>                                            # manifiesto + resumen (engine, n_trades, métricas)
ft_vectorbt runs --strategy-id <id> --limit 20                      # listado paginado de corridas (--cursor); filtros -e/-m/-s/-t
ft_vectorbt trades <run_id> --limit 100 --after-ms <next_after_ms>  # páginas de operaciones; curva de capital con `equity`
ft_vectorbt status <task_id> ; ft_vectorbt result <task_id>         # consulta de tareas lanzadas por la API
```

### Ejecución en Modo Servicio (FastAPI)
```bash
# Servicio HTTP único: /setup, /market-data/binance, /market-data/yfinance, /market-data/manager, /data, /datasets, /indicators, /ta-lib, /strategies, /backtest/vectorbt y /health (docs en /docs)
ft_serve --port 8000

# Yahoo Finance vía API: POST devuelve 202 con task_id y las URLs de polling
curl -X POST "http://localhost:8000/market-data/yfinance/download?symbols=AAPL&symbols=%5EGSPC&timeframes=1d&start=2024-01-01"
curl http://localhost:8000/market-data/yfinance/status/<task_id>  # también /plan/<task_id> y /result/<task_id> (409 si no terminó)

# Recetas de datasets vía API (mismo JSON que la CLI): GET /datasets/recipes[/{name}], POST /datasets/materialize/{name}
# (202; ?verify=true&refetch=false), GET /datasets/{status,plan,result}/<task_id>, POST /datasets/verify/{name}
# (200 con el estado en el cuerpo; 409 si los datos no son canónicos) y POST /datasets/lock/{name}?update=false (409 si se rechaza)
curl http://localhost:8000/datasets/recipes
curl -X POST http://localhost:8000/datasets/materialize/benchmark-v1
curl -X POST http://localhost:8000/datasets/verify/benchmark-v1

# Indicadores vía API (mismo contrato que la CLI): GET /indicators/catalog, POST /indicators/compute (202),
# GET /indicators/{status,plan,result}/<task_id>, GET /indicators/datasets/{exchange}/{market_type}/{symbol}/{timeframe}
# y POST /indicators/datasets/{...}/export (descarga en streaming)
curl http://localhost:8000/indicators/catalog
curl -X POST http://localhost:8000/indicators/compute -H "Content-Type: application/json" \
  -d '{"datasets":[{"exchange":"binance","market_type":"spot","symbol":"BTCUSDT","timeframe":"1h"}],"indicators":[{"name":"rsi"},{"name":"macd"}]}'
curl http://localhost:8000/indicators/status/<task_id>           # poll_url de la respuesta 202
curl -X POST http://localhost:8000/indicators/datasets/binance/spot/BTCUSDT/1h/export -H "Content-Type: application/json" \
  -d '{"format":"csv","include_ohlcv":true}' -o BTCUSDT_1h_indicators.csv

# TA-Lib vía API (mismas rutas bajo /ta-lib; GET /ta-lib/catalog?group=&function= filtra)
curl "http://localhost:8000/ta-lib/catalog?group=Pattern%20Recognition"
curl -X POST http://localhost:8000/ta-lib/compute -H "Content-Type: application/json"   -d '{"datasets":[{"exchange":"binance","market_type":"spot","symbol":"BTCUSDT","timeframe":"1h"}],"functions":[{"function":"kama"},{"function":"bbands","params":{"matype":"ema"}}],"groups":["Pattern Recognition"]}'
curl http://localhost:8000/ta-lib/status/<task_id>               # poll_url de la respuesta 202
curl -X POST http://localhost:8000/ta-lib/datasets/binance/spot/BTCUSDT/1h/export -H "Content-Type: application/json"   -d '{"format":"parquet","series":["kama","talib_cdlengulfing"]}' -o BTCUSDT_1h_ta_lib.parquet

# Estrategias vía API (mismo contrato que la CLI; X-User-ID opcional para auditoría)
curl -X POST http://localhost:8000/strategies -H "Content-Type: application/json" -H "X-User-ID: ana"   -d '{"name":"mean_reversion","parameters":{"fast":12,"slow":26},"tags":["fx"]}'      # 201 + Location: /strategies/<id>
curl "http://localhost:8000/strategies?tag=fx&name=mean&limit=50"                     # GET /strategies/<id>?version=1
curl -X PUT http://localhost:8000/strategies/<id> -H "Content-Type: application/json"   -d '{"parameters_patch":[{"op":"replace","path":"/fast","value":8}]}'                # nueva versión (400 si no hay cambios)
curl "http://localhost:8000/strategies/<id>/diff?from=1&to=2"                         # también /versions y /export?format=yaml
curl -X DELETE "http://localhost:8000/strategies/<id>?purge=false"                    # 204, idempotente

# Backtesting VectorBT vía API (mismo contrato que la CLI, Art. 1; despacho asíncrono 202 + polling)
curl -X POST http://localhost:8000/backtest/vectorbt/run -H "Content-Type: application/json" \
  -d '{"dataset":{"exchange":"binance","market_type":"futures","symbol":"BTCUSDT","timeframe":"1h"},"strategy_id":"<id>"}'   # 202 + status=ACCEPTED, poll_url, result_url
curl http://localhost:8000/backtest/vectorbt/status/<task_id>        # fase; GET /result/<task_id> (409 si no terminó, 404 inexistente, 500 si FAILED)
curl http://localhost:8000/backtest/vectorbt/kinds                   # estrategias instaladas (solo lectura; no existe endpoint de carga de código)
curl "http://localhost:8000/backtest/vectorbt/runs?strategy_id=<id>&limit=20"   # + /runs/<run_id> y /runs/<run_id>/{trades,equity}?after_ms=&limit=
```

---

## 4. Aseguramiento de Calidad y Tests

La suite aplica una política estricta de validación previa al commit. No se permite la integración de código que rompa el tipado estricto o las pruebas de regresión.

```bash
# Formateo y análisis estático rápido
python -m ruff check packages && python -m ruff format packages

# Lint, tipado estricto y tests: ejecutar DENTRO de cada paquete (cada uno tiene su pyproject.toml).
# ta-lib es dependencia de runtime de finances-analytics (ft_ta_lib); el contraste de ft_indicators
# lo usa en sus tests y finances-api valida ambas herramientas entre sí solo por HTTP.
for pkg in finances-core finances-downloader finances-analytics finances-backtester finances-api; do
  (cd packages/$pkg && python -m ruff check . && python -m ruff format --check . \
    && python -m mypy --strict src && python -m pytest -q)
done

# Benchmarks (excluidos por defecto; en CI solo en ubuntu-24.04). BENCH_OUTPUT guarda el JSON de
# medianas; BENCH_ENFORCE=1 aplica los umbrales de tests/<herramienta>/benchmarks/thresholds.json
# (ft_indicators RF-14 calibrado; ft_ta_lib RF-15 provisional hasta su calibración en CI).
(cd packages/finances-analytics && BENCH_OUTPUT=.temp/bench.json python -m pytest -m benchmark -s -q tests/ft_indicators/benchmarks)
(cd packages/finances-analytics && BENCH_OUTPUT=.temp/bench_ta_lib.json python -m pytest -m benchmark -s -q tests/ft_ta_lib/benchmarks)

# Suite opt-in de datos reales (marcador real_data, excluido por defecto: la suite normal no usa red).
# Requiere el almacén materializado y verificado; sin él los tests FALLAN con el detalle (no se omiten).
ft_datasets materialize benchmark-v1
(cd packages/finances-downloader && python -m pytest -m real_data -q)
(cd packages/finances-analytics && python -m pytest -m real_data -q)
```

En CI, la suite de datos reales vive en `.github/workflows/real-data.yml` (`ubuntu-24.04`): se lanza a mano
(*workflow_dispatch*), cada lunes a las 05:00 UTC o al poner la etiqueta **`real-data`** en un PR. Cachea
`FT_DATASETS_DIR` por hash de recetas + *locks* (solo tras una verificación `OK`), publica la tabla de `verify` en
el resumen del job y el JSON como artefacto. Si falla la ejecución semanal, `scripts/ci/real_data_issue.sh` abre (o
comenta, si ya hay uno abierto del mismo tipo) un issue `[real-data] <tipo>: benchmark-v1` con las etiquetas
`real-data` y `real-data:<download|drift|unlocked|tests|infra>`. Una deriva del proveedor se corrige con
`ft_datasets lock benchmark-v1 --update` en un PR revisado.

### Verificación local en Linux (WSL, opcional)

`scripts/wsl_ci.sh` repite los pasos de `ci.yml` en WSL (Ubuntu con `python3.12` y `rsync`) para detectar fallos
que solo aparecen en Linux antes del *push*, sin gastar una ejecución de GitHub. GitHub CI sigue siendo el control
obligatorio y los benchmarks quedan fuera (sus umbrales están calibrados en `ubuntu-24.04`).

```bash
wsl bash scripts/wsl_ci.sh                          # ruff + mypy --strict + pytest de todos los paquetes
wsl bash scripts/wsl_ci.sh --package finances-core  # solo un paquete (repetible)
# Tests sensibles a la concurrencia: un verde aislado no prueba nada, se repiten N veces
wsl bash scripts/wsl_ci.sh --package finances-core --stress 40 -- tests/unit/test_backtest_results_store.py -k concurrent
```

El repositorio se copia con `rsync` a `~/financial_tools-wsl` (leer `/mnt/c` es lento) y el entorno de Linux vive en
`~/.venvs/financial_tools`, nunca en `/tmp`, que WSL borra al reiniciar; el `.venv` de Windows no sirve porque sus
ruedas (numba, vectorbt, ta-lib) son de otra plataforma. Las instalaciones editables se rehacen solas cuando cambia
algún `pyproject.toml` (o con `--reinstall`). Desde Git Bash, anteponer `MSYS_NO_PATHCONV=1` si se pasan rutas
`/mnt/...`. Variables: `FT_WSL_WORKDIR`, `FT_WSL_VENV`, `FT_WSL_PYTHON`.
