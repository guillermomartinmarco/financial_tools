# Financial Tools Suite (`financial_tools`)

Suite corporativa de utilidades modulares financieras para el procesamiento de datos de mercado, análisis matemático de estrategias, optimización masiva en GPU y trading automatizado.

El proyecto está diseñado bajo la metodología **Spec-Driven Development (SDD)**, garantizando robustez mediante una estricta dualidad operativa: comandos efímeros en consola (CLI) y un demonio continuo de red (API HTTP asíncrona).

---

## 1. Arquitectura del Monorepo

Para garantizar el aislamiento y permitir ciclos SemVer independientes, las herramientas de la suite se agrupan en paquetes distribuidos de la siguiente manera (los marcados *(planificado)* aún no existen):

| Paquete Corporativo | Utilidades Asociadas (`ft_*`) | Stack Tecnológico Base |
| :--- | :--- | :--- |
| **`finances-core`** | `finances_core` (puente compartido: config, logging, tasks, schemas), `ft_setup`, `ft_strategy_manager` *(planificado)* | Pydantic Settings, Structlog, SQLite |
| **`finances-downloader`** | `ft_market_data_binance`, `ft_data_viewer`, `ft_market_data_yfinance` *(planificado)*, `ft_market_data_manager` *(planificado)* | Asyncio, Polars, Parquet |
| **`finances-api`** | `ft_serve` (raíz de composición: monta los routers de todas las herramientas) | FastAPI, Uvicorn |
| **`finances-backtester`** *(planificado)* | `ft_vectorbt`, `ft_backtesting_py`, `ft_backtrader`, `ft_bt`, `ft_ffn`, `ft_backtestopt_spectre` (C++/CUDA), `ft_bot_paper_binance`, `ft_live_bot` | VectorBT, Backtrader, PyBind11, CMake |

---

## 2. Mapa Detallado de Utilidades

### 2.1 Configuración y Gobernanza
*   **`ft_setup`**: Inicialización del entorno, diagnóstico de variables y enmascaramiento de llaves criptográficas/secretos usando emojis de advertencia (`⚠️`).
*   **`ft_strategy_manager`**: CRUD y persistencia formal de las reglas de parámetros de estrategias del usuario.

### 2.2 Captura y Gestión de Datos de Mercado
*   **`ft_market_data_binance`**: Descargador de alta velocidad asíncrono para la API de Binance (Spot/Futures).
*   **`ft_market_data_yfinance`**: Capturador de datos históricos OHLCV de Yahoo Finance.
*   **`ft_market_data_manager`**: Orquestador local de archivos y bases de datos históricas (conversión Parquet/Hive y SQLite).
*   **`ft_data_viewer`**: Explorador y diagnóstico de datasets OHLCV (listado, estadísticas, gaps, anomalías, exportación, comparación).

### 2.3 Motores de Backtesting y Análisis
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
python -m pip install -e packages/finances-api uvicorn   # servicio HTTP (ft_serve)
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

# Exploración y diagnóstico de datasets descargados
ft_data_viewer list                                    # Lista todos los datasets
ft_data_viewer summary -e binance -m spot -s BTCUSDT -t 1h  # Estadísticas
ft_data_viewer head -e binance -m spot -s BTCUSDT -t 1h -n 20  # Primeras 20 filas
ft_data_viewer gaps -e binance -m spot -s BTCUSDT -t 1h      # Gaps temporales
ft_data_viewer anomalies -e binance -m spot -s BTCUSDT -t 1h  # Duplicados, nulos, outliers
ft_data_viewer export -e binance -m spot -s BTCUSDT -t 1h --start 2024-01-01 --end 2024-06-01 -o ./subset  # Exportar
ft_data_viewer compare --exchange-a binance --market-a spot --symbol-a BTCUSDT --timeframe-a 1h \n  --exchange-b binance --market-b futures --symbol-b BTCUSDT --timeframe-b 1h  # Spot vs Futures
```

### Ejecución en Modo Servicio (FastAPI)
```bash
# Servicio HTTP único: /setup, /market-data/binance, /data y /health (docs en /docs)
ft_serve --port 8000
```

---

## 4. Aseguramiento de Calidad y Tests

La suite aplica una política estricta de validación previa al commit. No se permite la integración de código que rompa el tipado estricto o las pruebas de regresión.

```bash
# Formateo y análisis estático rápido
python -m ruff check packages && python -m ruff format packages

# Tipado estricto y tests: ejecutar DENTRO de cada paquete (cada uno tiene su pyproject.toml)
for pkg in finances-core finances-downloader finances-api; do
  (cd packages/$pkg && python -m mypy --strict src && python -m pytest -q)
done
```
