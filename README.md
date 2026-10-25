# Financial Tools Suite (`financial_tools`)

Suite corporativa de utilidades modulares financieras para el procesamiento de datos de mercado, análisis matemático de estrategias, optimización masiva en GPU y trading automatizado.

El proyecto está diseñado bajo la metodología **Spec-Driven Development (SDD)**, garantizando robustez mediante una estricta dualidad operativa: comandos efímeros en consola (CLI) y un demonio continuo de red (API HTTP asíncrona).

---

## 1. Arquitectura del Monorepo

Para garantizar el aislamiento y permitir ciclos SemVer independientes, las 13 herramientas de la suite se agrupan en 3 paquetes distribuidos de la siguiente manera:

| Paquete Corporativo | Utilidades Asociadas (`ft_*`) | Stack Tecnológico Base |
| :--- | :--- | :--- |
| **`finances-core`** | `ft_setup`, `common/`, `ft_strategy_manager` | Pydantic Settings, Structlog, SQLite |
| **`finances-downloader`** | `ft_market_data_binance`, `ft_market_data_yfinance`, `ft_market_data_manager` | Asyncio, Python standard, Polars, Parquet |
| **`finances-backtester`**| `ft_vectorbt`, `ft_backtesting_py`, `ft_backtrader`, `ft_bt`, `ft_ffn`, `ft_backtestopt_spectre` (C++/CUDA), `ft_bot_paper_binance`, `ft_live_bot`, `ft_api_service` | VectorBT, Backtrader, PyBind11, CMake, FastAPI |

---

## 2. Mapa Detallado de Utilidades (`src/`)

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
*   **`ft_api_service`**: El Daemon/Servidor central unificado. Expone endpoints web usando FastAPI y orquesta colas no bloqueantes delegando tareas al threadpool nativo mediante un `TaskManager` persistido en SQLite.

---

## 3. Guía de Inicio Rápido

> El workspace raíz con `uv` todavía no está creado; los comandos verificados hoy
> usan el entorno virtual del repositorio (`.venv`).

### Instalación en modo editable
```bash
python -m pip install -e packages/finances-core
python -m pip install -e packages/finances-downloader
```

### Ejecución en Modo Efímero (CLI)
```bash
# Inicialización del entorno (directorios, .env.template, tasks.db WAL)
ft_setup init

# Diagnóstico de variables FT_* (tabla Rich o JSON)
ft_setup doctor
ft_setup doctor --format json

# Descarga de datos históricos Binance (Public Data + REST fallback)
ft_market_data_binance download --symbol BTCUSDT --timeframe 1h --start 2024-01-01

# Exploración y diagnóstico de datasets descargados
ft_data_viewer list                                    # Lista todos los datasets
ft_data_viewer summary -e binance -m spot -s BTCUSDT -t 1h  # Estadísticas
ft_data_viewer head -e binance -m spot -s BTCUSDT -t 1h -n 20  # Primeras 20 filas
ft_data_viewer gaps -e binance -m spot -s BTCUSDT -t 1h      # Gaps temporales
ft_data_viewer anomalies -e binance -m spot -s BTCUSDT -t 1h  # Duplicados, nulos, outliers
ft_data_viewer export -e binance -m spot -s BTCUSDT -t 1h --start 2024-01-01 --end 2024-06-01 -o ./subset  # Exportar
```

### Ejecución en Modo Servicio (FastAPI)
```bash
# Servicio de setup (router /setup)
ft_setup serve --port 8000
```

---

## 4. Aseguramiento de Calidad y Tests

La suite aplica una política estricta de validación previa al commit. No se permite la integración de código que rompa el tipado estricto o las pruebas de regresión.

```bash
# Formateo y análisis estático rápido
python -m ruff check packages && python -m ruff format packages

# Chequeo estricto de tipos con MyPy (por paquete, usa su pyproject.toml)
cd packages/finances-core && python -m mypy --strict src

# Ejecución de la suite completa de pruebas (Unitarias e Integración)
python -m pytest packages/finances-core/tests
```
