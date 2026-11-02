# Spec — `ft_vectorbt` (MVP v1.0)

## Contexto e Historias de Usuario

`ft_vectorbt` es el primer motor de `finances-backtester`: ejecuta los tipos de estrategia del catálogo (`sma_cross`, `rsi_reversion`, `donchian_breakout`) con [VectorBT](https://vectorbt.dev) sobre un dataset del almacén Parquet y guarda trades, curva de capital y métricas en `FT_RESULTS_DIR`. Todo lo común (catálogo, semántica de señales, modelo de ejecución, formato de resultados, métricas, identidad, almacén, contrato de validación cruzada) está en `specs/finances_backtester/` y se implementa en `finances_core.backtest`; esta herramienta solo aporta el **adaptador** (`engine.py`) y los adaptadores CLI/API. VectorBT trabaja en `pandas`/NumPy de doble precisión: esa excepción queda confinada a `engine.py` por RFC-0004.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** `ft_vectorbt run` con una estrategia versionada, un dataset y un rango **para que** obtenga operaciones, equity y métricas en segundos incluso con millones de barras. |
| US-2 | Backtest Engineer | Como ingeniero, **exijo** que `ft_vectorbt` produzca exactamente las operaciones del *golden* de cada tipo **para que** sirva de base de contraste de los demás motores. |
| US-3 | Quant Researcher | Como investigador, **necesito** relanzar la misma corrida y recibir el resultado ya calculado **para que** no se repita trabajo. |
| US-4 | DevOps / Operador | Como operador, **quiero** `POST /backtest/vectorbt/run` con *polling* y la consulta de corridas almacenadas **para que** pueda integrarlo desde `ft_serve` sin bloquear. |
| US-5 | Auditor | Como auditor, **exijo** que el resultado registre la versión exacta de VectorBT y de la estrategia **para que** pueda reproducirlo. |

---

## Requisitos Funcionales (EARS)

Los términos (dataset, barra, `kind`, `run_id`, costes) son los de `specs/finances_backtester/spec.md`; los requisitos RF-1…RF-9 y RF-13/RF-14 de ese documento se aplican íntegros y aquí no se repiten.

### RF-1 — Adaptador de los Tipos del Catálogo (Ubícuo)
**EL SISTEMA DEBE** declarar `supported_kinds = {sma_cross, rsi_reversion, donchian_breakout}` y registrar en `ft_vectorbt/engine.py` (ADR-V03) una función por tipo que, dados los arreglos de velas de la frontera (RFC-0004) y los `params` validados, devuelva `entry`/`exit` booleanos con **exactamente** la semántica de `finances_backtester` RF-2.
- Las señales se calculan con funciones propias o con indicadores de VectorBT **solo si** reproducen la definición normativa (RSI de Wilder con semilla SMA, SMA simple, máximos/mínimos que excluyen la barra actual, cruces estrictos). **SI** un indicador de la biblioteca difiere (p. ej. otra semilla del RSI), **ENTONCES** se usa una implementación propia dentro de `engine.py`; nunca se cambia la semántica del catálogo para acomodar la biblioteca.
- **SI** se pide un tipo fuera de `supported_kinds`, **ENTONCES** `kind_not_supported_by_engine` (exit 2 / HTTP 400).
- **SI** `kind`, `params` o `strategy` son inválidos, **ENTONCES** los errores de `finances_backtester` RF-1/RF-3.

### RF-2 — Modelo de Ejecución en VectorBT (Ubícuo)
**EL SISTEMA DEBE** traducir las señales al modelo de `finances_backtester` RF-5 dentro de `engine.py`:
- Las señales se desplazan una barra (la de `t` se ejecuta en `t+1`); las entradas que caerían en la última barra se descartan.
- La ejecución usa el precio de apertura; el deslizamiento y la comisión se pasan como fracciones (`bps / 10 000`); solo largo, sin acumular posiciones y con tamaño `size_fraction` de la caja.
- **SI** hay posición abierta en la última barra, **ENTONCES** se fuerza una salida a su precio de cierre y las operaciones resultantes llevan `exit_reason = 1`.
- **EL SISTEMA DEBE** verificar con la suite de contrato (no asumir) cómo VectorBT descuenta comisiones al dimensionar (RF-5.5 del paquete); **SI** su semántica difiere, **ENTONCES** el adaptador ajusta el tamaño para cumplir la fórmula normativa.
- Las operaciones y la equity que devuelve VectorBT se convierten al formato canónico (`finances_backtester` RF-6) con `entry_time_ms`/`exit_time_ms` recuperados por posición del arreglo `int64` original.

### RF-3 — Frontera Numérica (Ubícuo, RFC-0004)
**EL SISTEMA DEBE** aislar `pandas`, NumPy de doble precisión y `vectorbt` en `ft_vectorbt/engine.py`, con exactamente dos puntos de conversión:
- `to_framework_input(candles)`: DataFrame Polars canónico → entrada de VectorBT (ampliación exacta); índice posicional, sin `datetime` ni zonas horarias.
- `from_framework_output(...)`: salida de VectorBT → `trades` y `equity` canónicos (`Float32`/`Int64`/`Int8`), con rechazo de valores no finitos o fuera de dominio (`output_out_of_range`, Art. 5.3, 13).
- Ningún otro módulo de `ft_vectorbt` importa `pandas`, `numpy` con tipos de doble precisión ni `vectorbt`, ni nombra tipos de doble precisión. Un test AST lo verifica (RFC-0004).
- Un test de precisión verifica la ida y vuelta sin pérdida de la entrada y la tolerancia de salida frente al *golden*.

### RF-4 — Pipeline de la Corrida (Ubícuo)
**EL SISTEMA DEBE** ejecutar, vía `finances_core.backtest.runner.run_backtest` con `VectorbtEngine`: resolver estrategia → cargar velas (solo lectura sobre `FT_PARQUET_DIR`) → `run_id` → ¿existe resultado? → simular → validar → métricas → persistir (`finances_backtester` RF-3…RF-9). `engine_version` es `vectorbt <versión instalada>`.
- La simulación se ejecuta en un hilo (`run_in_threadpool`/`asyncio.to_thread`, Art. 3); a lo sumo `FT_BACKTEST_MAX_CONCURRENT` corridas simultáneas por proceso, las demás esperan en cola.
- La fuente de velas es de **solo lectura**: **EL SISTEMA NO DEBE** escribir nunca bajo `FT_PARQUET_DIR`.

### RF-5 — Determinismo e Idempotencia (Ubícuo, Art. 12)
- Una misma petición (mismos datos, estrategia, costes y versión de VectorBT) **DEBE** producir `trades.parquet`, `equity.parquet` y `summary.json` idénticos byte a byte, en el mismo equipo y entre ejecuciones; entre Windows y Ubuntu, equivalentes dentro de la tolerancia de RFC-0004.
- **SI** existe el resultado y no hay `--force`, **ENTONCES** se devuelve sin invocar a VectorBT (`cached = true`); con `--force` se recalcula y el contenido debe seguir siendo idéntico.
- **SI** el hilo de simulación de VectorBT/Numba introduce no determinismo, **ENTONCES** se fija la configuración necesaria (p. ej. un solo hilo) y se documenta en el `plan.md`.

### RF-6 — Interfaz CLI (Ubícuo, Art. 1)
**EL SISTEMA DEBE** proveer el script `ft_vectorbt` (Typer) con flags globales `-v/--verbose`, `-q/--quiet`, `--log-format json|rich` y `--format table|json`:
- `run -d exchange/market_type/symbol/timeframe --strategy-id ID [--strategy-version N] [--start-ms] [--end-ms] [--initial-cash] [--size-fraction] [--fees-bps] [--slippage-bps] [--force] [--request-file FILE.json]` — síncrono con barra de progreso Rich; `--request-file` acepta exactamente el cuerpo de la API.
- `kinds` — tipos del catálogo y su esquema de parámetros.
- `runs [--exchange --market-type --symbol --timeframe --strategy-id] [--limit --cursor]`, `show RUN_ID`, `trades RUN_ID [--limit --after-ms]`, `equity RUN_ID [--limit --after-ms]` — lectura de resultados almacenados.
- `status TASK_ID`, `result TASK_ID` — consulta de tareas lanzadas por la API.
- Códigos de salida: RF-9.

### RF-7 — Interfaz API (Ubícuo, Art. 1, 18)
**EL SISTEMA DEBE** exponer, bajo el prefijo `/backtest/vectorbt` (`APIRouter` público montado por `finances-api`):
- `POST /run` (cuerpo `BacktestRequest`): valida, crea la tarea en `SQLTaskManager`, despacha en `BackgroundTasks` y responde `202` con `task_id` (UUID v7), `status="ACCEPTED"`, `poll_url` y `result_url` en ≤ 200 ms. La lectura de datos y el cálculo de `run_id` ocurren dentro de la tarea.
- `GET /status/{task_id}` (fase y avance), `GET /result/{task_id}` (`BacktestTaskResult`; `409` si no terminó, `404` si no existe, `500` si `FAILED`).
- `GET /kinds`, `GET /runs` (filtros y cursor), `GET /runs/{run_id}` (manifiesto + resumen, `404`), `GET /runs/{run_id}/trades` y `/equity` (paginados: `limit` 1..10000, `after_ms`, respuesta `items`, `count`, `next_after_ms`).
- La validación síncrona previa al `202` se limita a la forma de la petición (esquema, costes, rango); las comprobaciones que leen disco o la base (dataset, estrategia, tipo soportado) se hacen en la tarea o, si son baratas y deterministas (estrategia/tipo), antes del `202` para devolver `404`/`400` sin crear tarea.
- CLI y API comparten la misma lógica de servicio (Art. 1); un test de contrato compara ambas salidas para una misma petición.

### RF-8 — Despacho Asíncrono y Progreso (Estado-Driven)
**EL SISTEMA DEBE** registrar el progreso en `SQLTaskManager` (`share/tasks.db`, WAL) con las fases `PENDING → LOADING → SIMULATING → PERSISTING → COMPLETED` / `FAILED`, `task_id` creado al despachar y resumen (`run_id`, `cached`, `n_bars`, `n_trades`, errores). Los errores de validación se detectan antes de crear la tarea cuando es posible; `FAILED` solo ante una excepción inesperada del motor, `output_out_of_range` o error de IO.

### RF-9 — Errores y Códigos de Salida (Evento-Driven)
Se aplica la tabla de `finances_backtester` RF-14. Particularidades de esta herramienta:
| Condición | CLI exit | API |
|-----------|----------|-----|
| Tipo del catálogo no soportado por `ft_vectorbt` | 2 | 400 |
| `vectorbt` no instalable o versión incompatible detectada al arrancar | 3 | 500 |
| Excepción de VectorBT durante la simulación | 3 (tarea `FAILED`) | 500 en `/result` |
| `run` con un `run_id` ya existente y sin `--force` | 0 (`cached`) | 200 / 202 |

### RF-10 — Logging Dual y Auditoría (Ubícuo, Art. 9)
**EL SISTEMA DEBE** usar `configure_logging(component="ft_vectorbt")` y emitir el evento `backtest_run` de `finances_backtester` RF-13 con `engine="vectorbt"`. Contexto: `task_id`, `symbol`, `timeframe`, `dataset`, `run_id`, `kind`. Métricas: `load_ms`, `engine_ms`, `persist_ms`, `bars_per_s`. CLI: Rich en *stderr*; API: JSON en *stdout*.

### RF-11 — Configuración (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings`: `FT_PARQUET_DIR`, `FT_RESULTS_DIR`, `FT_TASKS_DB_PATH`, `FT_STRATEGIES_DB_PATH`, `FT_BACKTEST_MAX_CONCURRENT`. No introduce variables propias.

### RF-12 — Desempeño (Art. 20)
Sobre un dataset sintético de 1 000 000 de velas `1m` y `sma_cross(fast=20, slow=100)`, en `ubuntu-24.04`: el tiempo de `engine.run` (conversión + simulación + conversión) **DEBE** quedar bajo un umbral de regresión calibrado como en `ft_indicators` RF-14 (1,5 × mediana de 5 repeticiones en la primera ejecución real; después, regresión en CI). Benchmark excluido por defecto (`-m benchmark`), umbral en `tests/ft_vectorbt/benchmarks/thresholds.json`. Los umbrales concretos se fijan en la tarea de calibración, no se inventan aquí.

### RF-13 — Validación Cruzada (Ubícuo)
**EL SISTEMA DEBE** pasar la suite de contrato de `finances_backtester` RF-10 para los tres tipos sobre el *fixture* determinista: mismas operaciones y equity/métricas dentro de la tolerancia de RFC-0004. La suite se ejecuta en CI en `windows-latest` y `ubuntu-24.04`. Una discrepancia es un defecto del adaptador.

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Cortos, apalancamiento, multi-activo, optimización, bots, plugins/DSL, indicadores desde `ft_indicators`, gráficos** | **Fuera de alcance**; ver `specs/finances_backtester/spec.md`. |
| **Funciones de VectorBT no mapeadas al catálogo** (p. ej. `Portfolio.from_order_func`, optimización de parámetros por barrido) | **Fuera de alcance**: solo se ejecutan tipos del catálogo. |
| **Gráficos de VectorBT (Plotly)** | **Fuera de alcance**; las dependencias de visualización no se importan. |
| **Importar `pandas`/`vectorbt` fuera de `engine.py`** | **Prohibido** (RFC-0004, test AST). |
| **Importar otras `ft_*`, `finances_downloader`, `finances_analytics`** | **Prohibido** (Art. 4). La estrategia se lee con `finances_core.backtest.strategy_source`. |
| **Calcular métricas dentro de VectorBT** (`Portfolio.stats()`) | **Prohibido**: las métricas son las de `finances_core.backtest.metrics`. |
| **Versión de VectorBT** | **Pin a verificar en V0.2** (no se asume ninguna versión). |
