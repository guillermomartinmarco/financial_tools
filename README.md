# Financial Tools Suite (`financial_tools`)

Suite corporativa de utilidades modulares financieras para el procesamiento de datos de mercado, análisis matemático de estrategias, optimización masiva en GPU y trading automatizado.

El proyecto está diseñado bajo la metodología **Spec-Driven Development (SDD)**, garantizando robustez mediante una estricta dualidad operativa: comandos efímeros en consola (CLI) y un demonio continuo de red (API HTTP asíncrona).

---

## 1. Arquitectura del Monorepo

Para garantizar el aislamiento y permitir ciclos SemVer independientes, las herramientas de la suite se agrupan en paquetes distribuidos de la siguiente manera (los marcados *(planificado)* aún no existen):

| Paquete Corporativo | Utilidades Asociadas (`ft_*`) | Stack Tecnológico Base |
| :--- | :--- | :--- |
| **`finances-core`** | `finances_core` (puente compartido: config, logging, tasks, schemas), `ft_setup`, `ft_strategy_manager` *(planificado)* | Pydantic Settings, Structlog, SQLite |
| **`finances-downloader`** | `ft_market_data_binance`, `ft_market_data_yfinance`, `ft_data_viewer`, `ft_market_data_manager` *(planificado)* | Asyncio, Polars, Parquet |
| **`finances-analytics`** | `ft_indicators` (indicadores técnicos sobre OHLCV Hive), `ft_ta_lib` *(planificado)* | Polars (cálculo `Float64`, persistencia `Float32`), Parquet Hive |
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

### 2.3 Motores de Backtesting y Análisis
*   **`ft_indicators`** (paquete `finances-analytics`): Catálogo cerrado de 12 indicadores técnicos (`sma`, `ema`, `wma`, `rsi`, `macd`, `bb`, `atr`, `stoch`, `roc`, `obv`, `adx` y `vwap` móvil) implementados con expresiones nativas de Polars (sin TA-Lib ni pandas en ejecución; TA-Lib solo como contraste en los tests de CI). Recalcula la serie completa de cada dataset descargado (Binance o Yahoo) sin anticipación y alineado 1:1 con la fuente (mismo `open_time_ms`, calentamiento conservado como nulo), y persiste cada indicador de forma idempotente en `FT_INDICATORS_DIR/{exchange}/market_type=…/symbol=…/timeframe=…/year=…/month=…/{indicator_id}.parquet` (`Float32`). Publica `stable_from_ms` (desde qué vela el indicador deja de depender de su semilla) y tolerancias `f32` como oráculo de futuros kernels GPU. Exporta a Parquet, CSV o JSON Lines, opcionalmente junto con el OHLCV fuente.
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
python -m pip install -e packages/finances-analytics    # ft_indicators (con "[dev]" añade ta-lib para los tests)
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

# Indicadores técnicos sobre datasets ya descargados (dataset = exchange/market_type/symbol/timeframe)
ft_indicators catalog                                           # catálogo JSON (parámetros, calentamiento, stable_from, tolerancias f32)
ft_indicators compute -d binance/spot/BTCUSDT/1h -i rsi -i macd  # imprime el Task ID y las series calculadas
ft_indicators compute -d yfinance/equity/AAPL/1d -i sma:period=50,source=adj_close -i bb:std=2.5 --dry-run  # solo el plan
ft_indicators status <task_id>                                  # progreso (mismo JSON que GET /indicators/status)
ft_indicators result <task_id>                                  # resultado final (mismo JSON que GET /indicators/result)
ft_indicators show binance/spot/BTCUSDT/1h                      # manifiesto: indicadores calculados y stable_from_ms
ft_indicators export binance/spot/BTCUSDT/1h --include-ohlcv --format csv -o .temp/indicators  # tabla ancha a CSV
ft_indicators export binance/spot/BTCUSDT/1h -i rsi --stability drop --format jsonl -o -       # solo velas estables, a stdout
```

### Ejecución en Modo Servicio (FastAPI)
```bash
# Servicio HTTP único: /setup, /market-data/binance, /market-data/yfinance, /data, /indicators y /health (docs en /docs)
ft_serve --port 8000

# Yahoo Finance vía API: POST devuelve 202 con task_id y las URLs de polling
curl -X POST "http://localhost:8000/market-data/yfinance/download?symbols=AAPL&symbols=%5EGSPC&timeframes=1d&start=2024-01-01"
curl http://localhost:8000/market-data/yfinance/status/<task_id>  # también /plan/<task_id> y /result/<task_id> (409 si no terminó)

# Indicadores vía API (mismo contrato que la CLI): GET /indicators/catalog, POST /indicators/compute (202),
# GET /indicators/{status,plan,result}/<task_id>, GET /indicators/datasets/{exchange}/{market_type}/{symbol}/{timeframe}
# y POST /indicators/datasets/{...}/export (descarga en streaming)
curl http://localhost:8000/indicators/catalog
curl -X POST http://localhost:8000/indicators/compute -H "Content-Type: application/json" \
  -d '{"datasets":[{"exchange":"binance","market_type":"spot","symbol":"BTCUSDT","timeframe":"1h"}],"indicators":[{"name":"rsi"},{"name":"macd"}]}'
curl http://localhost:8000/indicators/status/<task_id>           # poll_url de la respuesta 202
curl -X POST http://localhost:8000/indicators/datasets/binance/spot/BTCUSDT/1h/export -H "Content-Type: application/json" \
  -d '{"format":"csv","include_ohlcv":true}' -o BTCUSDT_1h_indicators.csv
```

---

## 4. Aseguramiento de Calidad y Tests

La suite aplica una política estricta de validación previa al commit. No se permite la integración de código que rompa el tipado estricto o las pruebas de regresión.

```bash
# Formateo y análisis estático rápido
python -m ruff check packages && python -m ruff format packages

# Lint, tipado estricto y tests: ejecutar DENTRO de cada paquete (cada uno tiene su pyproject.toml).
# finances-analytics requiere el extra [dev] (ta-lib: contraste de ft_indicators).
for pkg in finances-core finances-downloader finances-analytics finances-api; do
  (cd packages/$pkg && python -m ruff check . && python -m ruff format --check . \
    && python -m mypy --strict src && python -m pytest -q)
done

# Benchmarks de ft_indicators (RF-14; excluidos por defecto, en CI solo en ubuntu-latest).
# BENCH_OUTPUT guarda el JSON de medianas; BENCH_ENFORCE=1 aplica los umbrales de
# tests/ft_indicators/benchmarks/thresholds.json.
(cd packages/finances-analytics && BENCH_OUTPUT=.temp/bench.json python -m pytest -m benchmark -s -q)
```
