# Spec — `ft_vectorbt` (MVP v1.0)

## Contexto e Historias de Usuario

`ft_vectorbt` es el primer motor de `finances-backtester`: simula con [VectorBT](https://vectorbt.dev) las señales de cualquier estrategia registrada (integradas `sma_cross`, `rsi_reversion`, `donchian_breakout` y las locales de `FT_STRATEGIES_DIR`) sobre un dataset del almacén Parquet y guarda trades, curva de capital y métricas en `FT_RESULTS_DIR`. Todo lo común (registro y contrato de estrategias, **cálculo de señales en el núcleo con Polars**, modelo de ejecución, formato de resultados, métricas, identidad, almacén, contrato de validación cruzada) está en `specs/finances_backtester/` y se implementa en `finances_core.backtest`; esta herramienta **solo simula**: aporta el **adaptador** (`engine.py`), que recibe `entries`/`exits` ya calculadas y no conoce `kind` ni parámetros, y los adaptadores CLI/API. VectorBT trabaja en `pandas`/NumPy de doble precisión: esa excepción queda confinada a `engine.py` por RFC-0004.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** `ft_vectorbt run` con una estrategia versionada, un dataset y un rango **para que** obtenga operaciones, equity y métricas en segundos incluso con millones de barras. |
| US-2 | Backtest Engineer | Como ingeniero, **exijo** que `ft_vectorbt` produzca exactamente las operaciones del *golden* de cada estrategia integrada **para que** sirva de base de contraste de los demás motores. |
| US-3 | Quant Researcher | Como investigador, **necesito** relanzar la misma corrida y recibir el resultado ya calculado **para que** no se repita trabajo. |
| US-4 | DevOps / Operador | Como operador, **quiero** `POST /backtest/vectorbt/run` con *polling* y la consulta de corridas almacenadas **para que** pueda integrarlo desde `ft_serve` sin bloquear. |
| US-5 | Auditor | Como auditor, **exijo** que el resultado registre la versión exacta de VectorBT y de la estrategia **para que** pueda reproducirlo. |

---

## Requisitos Funcionales (EARS)

Los términos (dataset, barra, `kind`, `Signals`, `run_id`, costes) son los de `specs/finances_backtester/spec.md`; los requisitos RF-1…RF-9, RF-13/RF-14 y RF-16…RF-22 de ese documento (registro y contrato de estrategias, señales en el núcleo, descubrimiento local, reproducibilidad, neutralidad del motor) se aplican íntegros y aquí no se repiten.

### RF-1 — Entrada del Motor: Señales Precalculadas (Ubícuo) *(reescrito: sustituye al «Adaptador de los Tipos del Catálogo»)*
**EL SISTEMA DEBE** implementar `VectorbtEngine.run(candles, signals, costs, size_fraction, initial_cash)` de `finances_backtester` RF-10, que recibe `Signals(entries, exits)` ya calculadas y normalizadas por el núcleo (RF-16 de `finances_backtester`) y las simula sin interpretarlas.
- **EL SISTEMA NO DEBE** calcular señales ni indicadores, ni conocer `kind`, `params` o la estrategia dentro de `ft_vectorbt`; no existen `KINDS_IMPL`, `supported_kinds` ni código por estrategia (ADR-V03 sustituido; RF-21 de `finances_backtester`). Cualquier estrategia registrada en el núcleo funciona sin tocar este motor.
- `to_framework_input` convierte además `entries`/`exits` (`Boolean` de Polars) a arreglos booleanos del framework; es una conversión sin pérdida y no introduce doble precisión.
- **SI** `kind`, `params` o `strategy` son inválidos, o la estrategia local falla al inicializarse o al calcular señales, **ENTONCES** los errores de `finances_backtester` RF-1/RF-3/RF-14 (`unknown_kind`, `invalid_params`, `strategy_init_error`, `strategy_runtime_error`, `invalid_signals`); `ft_vectorbt` no añade errores de tipo no soportado.

### RF-2 — Modelo de Ejecución en VectorBT (Ubícuo)
**EL SISTEMA DEBE** traducir las señales al modelo de `finances_backtester` RF-5 dentro de `engine.py`:
- Las señales recibidas (al cierre de `t`) se desplazan una barra (la de `t` se ejecuta en `t+1`); las entradas que caerían en la última barra se descartan.
- La ejecución usa el precio de apertura; el deslizamiento y la comisión se pasan como fracciones (`bps / 10 000`); solo largo, sin acumular posiciones y con tamaño `size_fraction` de la caja.
- **SI** hay posición abierta en la última barra, **ENTONCES** se fuerza una salida a su precio de cierre y las operaciones resultantes llevan `exit_reason = 1`.
- **EL SISTEMA DEBE** verificar con la suite de contrato (no asumir) cómo VectorBT descuenta comisiones al dimensionar (RF-5.5 del paquete); **SI** su semántica difiere, **ENTONCES** el adaptador ajusta el tamaño para cumplir la fórmula normativa.
- Las operaciones y la equity que devuelve VectorBT se convierten al formato canónico (`finances_backtester` RF-6) con `entry_time_ms`/`exit_time_ms` recuperados por posición del arreglo `int64` original.

### RF-3 — Frontera Numérica (Ubícuo, RFC-0004)
**EL SISTEMA DEBE** aislar `pandas`, NumPy de doble precisión y `vectorbt` en `ft_vectorbt/engine.py`, con exactamente dos puntos de conversión:
- `to_framework_input(candles, signals)`: DataFrame Polars canónico y `Signals` → entrada de VectorBT (ampliación exacta de `Float32`; booleanos sin pérdida); índice posicional, sin `datetime` ni zonas horarias.
- `from_framework_output(...)`: salida de VectorBT → `trades` y `equity` canónicos (`Float32`/`Int64`/`Int8`), con rechazo de valores no finitos o fuera de dominio (`output_out_of_range`, Art. 5.3, 13).
- Ningún otro módulo de `ft_vectorbt` importa `pandas`, `numpy` con tipos de doble precisión ni `vectorbt`, ni nombra tipos de doble precisión. Un test AST lo verifica (RFC-0004).
- Un test de precisión verifica la ida y vuelta sin pérdida de la entrada y la tolerancia de salida frente al *golden*.

### RF-4 — Pipeline de la Corrida (Ubícuo)
**EL SISTEMA DEBE** ejecutar, vía `finances_core.backtest.runner.run_backtest` con `VectorbtEngine`: resolver estrategia → cargar velas (solo lectura sobre `FT_PARQUET_DIR`) → `run_id` → ¿existe resultado? → calcular señales en el núcleo → simular → validar → métricas → persistir (`finances_backtester` RF-3…RF-9). `engine_version` es `vectorbt <versión instalada>`.
- Las señales (núcleo) y la simulación se ejecutan en un hilo (`run_in_threadpool`/`asyncio.to_thread`, Art. 3); a lo sumo `FT_BACKTEST_MAX_CONCURRENT` corridas simultáneas por proceso, las demás esperan en cola.
- La fuente de velas es de **solo lectura**: **EL SISTEMA NO DEBE** escribir nunca bajo `FT_PARQUET_DIR`.

### RF-5 — Determinismo e Idempotencia (Ubícuo, Art. 12)
- Una misma petición (mismos datos, estrategia, costes y versión de VectorBT) **DEBE** producir `trades.parquet`, `equity.parquet` y `summary.json` idénticos byte a byte, en el mismo equipo y entre ejecuciones; entre Windows y Ubuntu, equivalentes dentro de la tolerancia de RFC-0004.
- **SI** existe el resultado y no hay `--force`, **ENTONCES** se devuelve sin invocar a VectorBT (`cached = true`); con `--force` se recalcula y el contenido debe seguir siendo idéntico.
- **SI** el hilo de simulación de VectorBT/Numba introduce no determinismo, **ENTONCES** se fija la configuración necesaria (p. ej. un solo hilo) y se documenta en el `plan.md`.

### RF-6 — Interfaz CLI (Ubícuo, Art. 1)
**EL SISTEMA DEBE** proveer el script `ft_vectorbt` (Typer) con flags globales `-v/--verbose`, `-q/--quiet`, `--log-format json|rich` y `--format table|json`:
- `run -d exchange/market_type/symbol/timeframe --strategy-id ID [--strategy-version N] [--start] [--end] [--initial-cash] [--size-fraction] [--fees-bps] [--slippage-bps] [--force] [--request-file FILE.json]` — síncrono con barra de progreso Rich; `--request-file` acepta exactamente el cuerpo de la API.
- `kinds [--kind KIND] [--check]` — estrategias registradas (integradas y locales, incluidas las carpetas inválidas con su error), versión, `code_hash` y esquema de parámetros; `--check` (solo CLI) ejecuta `assert_strategy_contract` sobre las locales (`finances_backtester` RF-20, A25) y sale con exit 2 si alguna falla.
- `runs [--exchange --market-type --symbol --timeframe --strategy-id] [--limit --cursor]`, `show RUN_ID`, `trades RUN_ID [--limit --after-ms]`, `equity RUN_ID [--limit --after-ms]` — lectura de resultados almacenados.
- `status TASK_ID`, `result TASK_ID` — consulta de tareas lanzadas por la API.
- **SI** se indica el rango con `--start`/`--end` (o el cuerpo de la API `start`/`end`), **ENTONCES** se acepta en formato flexible — ISO 8601 (`2024-01-01`, `2024-01-01T00:00:00Z`), *ms epoch* (`1704067200000`) o `now` — y se convierte a ms UTC con `finances_core.timeutils.parse_time_input` (puente del núcleo, Art. 4, el mismo helper que los descargadores). **SI** el valor no es parseable, **ENTONCES** es error de validación (CLI exit 2, API `422`). Estas opciones **reemplazan** a `--start-ms`/`--end-ms` (cambio incompatible, alcanza también a `ft_vectorbt optimize`); como se admite *ms epoch*, el formato antiguo sigue siendo expresable.
- Códigos de salida: RF-9.

### RF-7 — Interfaz API (Ubícuo, Art. 1, 18)
**EL SISTEMA DEBE** exponer, bajo el prefijo `/backtest/vectorbt` (`APIRouter` público montado por `finances-api`):
- `POST /run` (cuerpo `BacktestRequest`): valida, crea la tarea en `SQLTaskManager`, despacha en `BackgroundTasks` y responde `202` con `task_id` (UUID v7), `status="ACCEPTED"`, `poll_url` y `result_url` en ≤ 200 ms. La lectura de datos y el cálculo de `run_id` ocurren dentro de la tarea.
- `GET /status/{task_id}` (fase y avance), `GET /result/{task_id}` (`BacktestTaskResult`; `409` si no terminó, `404` si no existe, `500` si `FAILED`).
- `GET /kinds` (lista las estrategias ya instaladas, `KindInfo`; **no existe ningún endpoint que reciba código, rutas ni módulos**, RF-18 de `finances_backtester`), `GET /runs` (filtros y cursor), `GET /runs/{run_id}` (manifiesto + resumen, `404`), `GET /runs/{run_id}/trades` y `/equity` (paginados: `limit` 1..10000, `after_ms`, respuesta `items`, `count`, `next_after_ms`).
- La validación síncrona previa al `202` se limita a la forma de la petición (esquema, costes, rango); las comprobaciones que leen disco o la base (dataset, estrategia) se hacen en la tarea o, si son baratas y deterministas (estrategia, `kind` registrado y `params` válidos), antes del `202` para devolver `404`/`400` sin crear tarea.
- CLI y API comparten la misma lógica de servicio (Art. 1); un test de contrato compara ambas salidas para una misma petición.

### RF-8 — Despacho Asíncrono y Progreso (Estado-Driven)
**EL SISTEMA DEBE** registrar el progreso en `SQLTaskManager` (`share/tasks.db`, WAL) con las fases `PENDING → LOADING → SIGNALS → SIMULATING → PERSISTING → COMPLETED` / `FAILED`, `task_id` creado al despachar y resumen (`run_id`, `cached`, `n_bars`, `n_trades`, errores). Los errores de validación se detectan antes de crear la tarea cuando es posible; `FAILED` solo ante una excepción inesperada del motor, `output_out_of_range` o error de IO.

### RF-9 — Errores y Códigos de Salida (Evento-Driven)
Se aplica la tabla de `finances_backtester` RF-14. Particularidades de esta herramienta:
| Condición | CLI exit | API |
|-----------|----------|-----|
| Carpeta de estrategia local inválida o con colisión (`strategy_init_error`) | 2 | 500 |
| `vectorbt` no instalable o versión incompatible detectada al arrancar | 3 | 500 |
| Excepción de VectorBT durante la simulación | 3 (tarea `FAILED`) | 500 en `/result` |
| Excepción dentro de `Strategy.signals` o señales inválidas (`strategy_runtime_error`, `invalid_signals`) | 3 (tarea `FAILED`) | 500 en `/result` |
| `run` con un `run_id` ya existente y sin `--force` | 0 (`cached`) | 200 / 202 |

### RF-10 — Logging Dual y Auditoría (Ubícuo, Art. 9)
**EL SISTEMA DEBE** usar `configure_logging(component="ft_vectorbt")` y emitir el evento `backtest_run` de `finances_backtester` RF-13 con `engine="vectorbt"`. Contexto: `task_id`, `symbol`, `timeframe`, `dataset`, `run_id`, `kind`, `strategy_code_hash`. Métricas: `load_ms`, `signals_ms`, `engine_ms`, `persist_ms`, `bars_per_s`. CLI: Rich en *stderr*; API: JSON en *stdout*.

### RF-11 — Configuración (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings`: `FT_PARQUET_DIR`, `FT_RESULTS_DIR`, `FT_TASKS_DB_PATH`, `FT_STRATEGIES_DB_PATH`, `FT_BACKTEST_MAX_CONCURRENT`, `FT_STRATEGIES_DIR` (consumido por el núcleo al descubrir estrategias; ni la CLI ni la API aceptan un directorio alternativo). No introduce variables propias.

### RF-12 — Desempeño (Art. 20)
Sobre un dataset sintético de 1 000 000 de velas `1m` y `sma_cross(fast=20, slow=100)`, en `ubuntu-24.04`: el tiempo de `engine.run` (conversión + simulación + conversión) con las señales de `sma_cross` ya calculadas por el núcleo (fuera del cronómetro; su benchmark es del núcleo) **DEBE** quedar bajo un umbral de regresión calibrado como en `ft_indicators` RF-14 (1,5 × mediana de 5 repeticiones en la primera ejecución real; después, regresión en CI). Benchmark excluido por defecto (`-m benchmark`), umbral en `tests/ft_vectorbt/benchmarks/thresholds.json`. Los umbrales concretos se fijan en la tarea de calibración, no se inventan aquí.

### RF-13 — Validación Cruzada (Ubícuo)
**EL SISTEMA DEBE** pasar la suite de contrato de `finances_backtester` RF-10 para las tres estrategias integradas y para la estrategia sintética de prueba (RF-21) sobre el *fixture* determinista: mismas operaciones y equity/métricas dentro de la tolerancia de RFC-0004. La suite se ejecuta en CI en `windows-latest` y `ubuntu-24.04`. Una discrepancia es un defecto del adaptador.

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Cortos, apalancamiento, multi-activo, optimización, bots, DSL/fórmulas, estrategias con estado de ejecución, indicadores desde `ft_indicators`, gráficos** | **Fuera de alcance**; ver `specs/finances_backtester/spec.md`. |
| **Funciones de VectorBT no mapeadas al contrato de señales** (p. ej. `Portfolio.from_order_func`, optimización de parámetros por barrido) | **Fuera de alcance**: solo se simulan `entries`/`exits` booleanas del núcleo. |
| **Gráficos de VectorBT (Plotly)** | **Fuera de alcance**; las dependencias de visualización no se importan. |
| **Importar `pandas`/`vectorbt` fuera de `engine.py`** | **Prohibido** (RFC-0004, test AST). |
| **Calcular señales o importar `finances_core.backtest.strategies`/`catalog` en `ft_vectorbt`** | **Prohibido** (RF-1; `finances_backtester` RF-15/RF-21, test AST). |
| **Importar otras `ft_*`, `finances_downloader`, `finances_analytics`** | **Prohibido** (Art. 4). La estrategia se lee con `finances_core.backtest.strategy_source`. |
| **Calcular métricas dentro de VectorBT** (`Portfolio.stats()`) | **Prohibido**: las métricas son las de `finances_core.backtest.metrics`. |
| **Versión de VectorBT** | **Pin a verificar en V0.2** (no se asume ninguna versión). |
