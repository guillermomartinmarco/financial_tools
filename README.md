# Financial Tools Suite (`financial_tools`)

Suite corporativa de utilidades modulares financieras para el procesamiento de datos de mercado, análisis matemático de estrategias, optimización masiva en GPU y trading automatizado.

El proyecto está diseñado bajo la metodología **Spec-Driven Development (SDD)**, garantizando robustez mediante una estricta dualidad operativa: comandos efímeros en consola (CLI) y un demonio continuo de red (API HTTP asíncrona).

---

## 1. Arquitectura del Monorepo

Para garantizar el aislamiento y permitir ciclos SemVer independientes, las herramientas de la suite se agrupan en paquetes distribuidos de la siguiente manera (los marcados *(planificado)* aún no existen):

| Paquete Corporativo | Utilidades Asociadas (`ft_*`) | Stack Tecnológico Base |
| :--- | :--- | :--- |
| **`finances-core`** | `finances_core` (puente compartido: config, logging, tasks, schemas), `ft_setup`, `ft_strategy_manager` *(planificado)* | Pydantic Settings, Structlog, SQLite |
| **`finances-downloader`** | `ft_market_data_binance`, `ft_market_data_yfinance`, `ft_data_viewer`, `ft_datasets` (recetas de datasets reales reproducibles), `ft_market_data_manager` *(planificado)* | Asyncio, Polars, Parquet |
| **`finances-analytics`** | `ft_indicators` (indicadores técnicos sobre OHLCV Hive), `ft_ta_lib` (catálogo completo de TA-Lib sobre OHLCV Hive) | Polars (cálculo `Float64`, persistencia `Float32`), TA-Lib (rueda binaria), Parquet Hive |
| **`finances-api`** | `ft_serve` (raíz de composición: monta los routers de todas las herramientas) | FastAPI, Uvicorn |
| **`finances-backtester`** *(planificado)* | `ft_vectorbt`, `ft_backtesting_py`, `ft_backtrader`, `ft_bt`, `ft_ffn`, `ft_backtestopt_spectre` (C++/CUDA), `ft_bot_paper_binance`, `ft_live_bot` | VectorBT, Backtrader, PyBind11, CMake |

---

## 2. Mapa Detallado de Utilidades

### 2.1 Configuración y Gobernanza
*   **`ft_setup`**: Inicialización del entorno, diagnóstico de variables y enmascaramiento de llaves criptográficas/secretos usando emojis de advertencia (`⚠️`).
*   **`ft_strategy_manager`**: CRUD y persistencia formal de las reglas de parámetros de estrategias del usuario.

### 2.2 Captura y Gestión de Datos de Mercado
*   **`ft_market_data_binance`**: Descargador de alta velocidad asíncrono para la API de Binance (Spot/Futures).
*   **`ft_market_data_yfinance`**: Descargador incremental y sin pérdida de OHLCV de Yahoo Finance (acciones, ETFs, índices, forex, futuros y cripto contra fiat; timeframes `1m`, `5m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M`). Respeta los límites de historial intradía de Yahoo (`1m` 30 días, `5m`–`30m` 60 días, `1h` 730 días; lo que queda fuera se informa en `gaps_unfillable`), no trata los cierres de mercado como huecos y detecta el `market_type` (`equity`, `etf`, `index`, `fx`, `crypto`, `future`, `fund`, `other`) a partir de Yahoo. Guarda el OHLC tal como lo entrega Yahoo (**ajustado por splits, no por dividendos**) más `adj_close` en `yfinance/market_type=…/symbol=…/timeframe=…/year=…/month=…/data.parquet`, y los dividendos/splits/capital gains en `yfinance_events/symbol=…/events.parquet` (permite reconstruir el precio cotizado y el retorno total). No requiere API key.
*   **`ft_market_data_manager`**: Orquestador local de archivos y bases de datos históricas (conversión Parquet/Hive y SQLite).
*   **`ft_data_viewer`**: Explorador y diagnóstico de datasets OHLCV (listado, estadísticas, gaps, anomalías, exportación, comparación).
*   **`ft_datasets`**: Recetas de **datasets reales reproducibles** para probar masivamente con datos reales. Una receta versionada (`datasets/<nombre>.toml`) fija exchange, `market_type`, símbolos, timeframes y rangos **cerrados y pasados** (`end` exclusivo y al menos `FT_DATASETS_SETTLE_DAYS` días atrás; en Yahoo solo `1d`/`1w`/`1M`); su *lock* (`datasets/<nombre>.lock.json`) fija el hash de contenido canónico `ftds-v1` de cada ítem (bits exactos de `float32`/`int64`: independiente del escritor Parquet, del SO y de la CPU). `materialize` invoca a los descargadores **por su CLI pública como subprocesos** (nunca los importa) sobre el almacén dedicado `FT_DATASETS_DIR` y omite sin red los ítems que ya verifican; `verify` (local, sin red) distingue `OK`, `MISSING`, `MISMATCH` (deriva `ROWS_CHANGED` / `SCALE_RESTATED` con factor / `VALUES_CHANGED`, columnas y meses afectados) y `UNLOCKED`; `lock` escribe el *lock* solo de forma explícita (`--update` para re-bloquear tras una re-expresión del proveedor, en un PR revisado). Receta inicial: `benchmark-v1` (BTCUSDT/ETHUSDT `1m`+`1h` Q1 2024 y AAPL, ^GSPC, EURUSD=X `1d` 2015-2024; ~274 k filas, ~9 MB).

### 2.3 Motores de Backtesting y Análisis
*   **`ft_indicators`** (paquete `finances-analytics`): Catálogo cerrado de 12 indicadores técnicos (`sma`, `ema`, `wma`, `rsi`, `macd`, `bb`, `atr`, `stoch`, `roc`, `obv`, `adx` y `vwap` móvil) implementados con expresiones nativas de Polars (sin TA-Lib ni pandas en ejecución; TA-Lib solo como contraste en los tests de CI). Recalcula la serie completa de cada dataset descargado (Binance o Yahoo) sin anticipación y alineado 1:1 con la fuente (mismo `open_time_ms`, calentamiento conservado como nulo), y persiste cada indicador de forma idempotente en `FT_INDICATORS_DIR/{exchange}/market_type=…/symbol=…/timeframe=…/year=…/month=…/{indicator_id}.parquet` (`Float32`). Publica `stable_from_ms` (desde qué vela el indicador deja de depender de su semilla) y tolerancias `f32` como oráculo de futuros kernels GPU. Exporta a Parquet, CSV o JSON Lines, opcionalmente junto con el OHLCV fuente.
*   **`ft_ta_lib`** (paquete `finances-analytics`): Envoltura delgada sobre **todo** el catálogo de TA-Lib (ta-lib 0.8.2 / TA-Lib C 0.8.1: 201 funciones en 10 grupos, 200 soportadas; `MAVP` no, por requerir la entrada `periods`), descubierto en tiempo de ejecución con `talib.abstract`. Mismo contrato de datos que `ft_indicators` (alineación 1:1 por `open_time_ms`, calentamiento nulo, sin anticipación, recálculo completo idempotente) y mismo `FT_INDICATORS_DIR`, con prefijo `talib_` y manifiesto/bloqueo propios (`_manifest.ft_ta_lib.json`, `_lock.ft_ta_lib`). Salidas reales en `Float32`, patrones de velas y categóricas en `Int16` (±200) e índices (`MAXINDEX`, …) como `open_time_ms` `Int64` (RFC-0002). Nombres deterministas con todos los parámetros (`talib_rsi_14`, `talib_bbands_20_2_2_sma_upperband`, `talib_cdlengulfing`); las funciones con *unstable period* de TA-Lib publican `stable_from_ms` nulo (`unstable_period_unknown`). Exporta igual que `ft_indicators` y se valida contra ella en CI.
*   **`ft_vectorbt` / `ft_backtesting_py` / `ft_backtrader` / `ft_bt`**: Envolturas modulares que exponen e interconectan los frameworks tradicionales al formato de datos canónico de la suite.
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
ft_data_viewer export -e binance -m spot -s BTCUSDT -t 1h --start 2024-01-01 --end 2024-06-01 -o ./subset  # Exportar
ft_data_viewer summary -e yfinance -m equity -s AAPL -t 1d  # Dataset de Yahoo (market_type detectado)
ft_data_viewer compare --exchange-a binance --market-a spot --symbol-a BTCUSDT --timeframe-a 1h \
  --exchange-b binance --market-b futures --symbol-b BTCUSDT --timeframe-b 1h  # Spot vs Futures

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
```

### Ejecución en Modo Servicio (FastAPI)
```bash
# Servicio HTTP único: /setup, /market-data/binance, /market-data/yfinance, /data, /datasets, /indicators, /ta-lib y /health (docs en /docs)
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
for pkg in finances-core finances-downloader finances-analytics finances-api; do
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
