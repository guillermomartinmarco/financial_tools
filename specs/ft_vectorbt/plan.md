# Plan — `ft_vectorbt` (MVP v1.0)

## 1. Modelo de Datos y Contratos

### 1.1 Estructura
```
packages/finances-backtester/src/finances_backtester/ft_vectorbt/
  __init__.py      # __version__ y ENGINE_NAME = "vectorbt"
  engine.py        # ÚNICO módulo con pandas/NumPy de doble precisión/vectorbt (RFC-0004)
  service.py       # arma VectorbtEngine y delega en finances_core.backtest.runner.run_backtest; dispatch de tareas
  cli.py           # Typer
  api.py           # APIRouter(prefix="/backtest/vectorbt")
tests/ft_vectorbt/ # test_isolation.py, test_engine_boundary.py, test_engine_precision.py, test_engine_signals_agnostic.py,
                   # test_service.py, test_cli.py, test_api.py, test_contract.py, test_contract_golden.py, benchmarks/
tests/contract/    # fixture, referencia de prueba, golden y engine_contract.py (specs/finances_backtester plan §5)
```
Esquemas, registro y contrato de estrategias, cálculo de señales, contrato de motor, runner, métricas, identidad y almacén: `finances_core.backtest` / `schemas.backtest` (`specs/finances_backtester/plan.md` §1). Esta herramienta no define esquemas propios ni estrategias.

### 1.2 Configuración
Sin variables nuevas (RF-11). `ENGINE_NAME = "vectorbt"`; `engine_version = f"vectorbt {vectorbt.__version__}"`.

## 2. Mapeo de Interfaces

### 2.1 CLI (`ft_vectorbt`)
| Comando | Argumentos principales | Salida / exit |
|---|---|---|
| `run` | `-d ex/mt/sym/tf`, `--strategy-id`, `--strategy-version`, `--start`, `--end`, `--initial-cash`, `--size-fraction`, `--fees-bps`, `--slippage-bps`, `--force`, `--request-file` | resumen (tabla o JSON), `run_id`, `cached`; exit RF-9 |

**Rango temporal flexible (CLI + API).** `--start`/`--end` y los campos `start`/`end` del cuerpo `BacktestRequest` son strings que aceptan ISO 8601, *ms epoch* o `now`, convertidos a ms UTC con `finances_core.timeutils.parse_time_input` (puente del núcleo, Art. 4). `BacktestRequest` deriva `start_ms`/`end_ms` (propiedades en ms) a partir de `start`/`end`, de modo que el `runner` y el manifiesto (rango resuelto en ms) no cambian; el validador rechaza valores no parseables (API `422`, CLI exit 2). `ft_vectorbt optimize` recibe las mismas `--start`/`--end` y las convierte a ms para los folds. Reemplaza a `--start-ms`/`--end-ms`.
| `kinds` | `--kind`, `--check` | estrategias registradas (`KindInfo`, incluidas carpetas locales inválidas); `--check` ejecuta `assert_strategy_contract` (solo CLI) |

**Deriva (Art. 15) — acceso al registro sin romper el aislamiento (RF-21):** `kinds`/`--check` no pueden importar `finances_core.backtest.catalog` ni `strategies` (prohibidos en todo el paquete por `test_isolation.py`). Llegan al registro por la puerta sancionada `finances_core.backtest.strategy_source`, que se amplía con dos funciones aditivas: `kinds(settings) -> list[KindInfo]` (envuelve `get_registry(settings).kinds()`) y `check_contract(kind, settings)` (envuelve `assert_strategy_contract`). La CLI/API las consumen vía un paso delgado en `service.py`.
| `runs` | filtros, `--limit`, `--cursor` | listado paginado |
| `show` / `trades` / `equity` | `RUN_ID` (+ `--limit`, `--after-ms`) | manifiesto+resumen / páginas |
| `status` / `result` | `TASK_ID` | estado / `BacktestTaskResult` |

### 2.2 API (prefijo `/backtest/vectorbt`)
| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/run` | `202` `BacktestDispatchResponse` · 400 · 404 · 422 |
| GET | `/status/{task_id}` | estado y fase · 404 |
| GET | `/result/{task_id}` | `BacktestTaskResult` · 404 · 409 · 500 |
| GET | `/kinds` | estrategias instaladas (`KindInfo`); solo lectura, sin carga de código |
| GET | `/runs` | `RunsPage` (`exchange`, `market_type`, `symbol`, `timeframe`, `strategy_id`, `limit`, `cursor`) |
| GET | `/runs/{run_id}` | manifiesto + resumen · 404 |
| GET | `/runs/{run_id}/trades` | `TradesPage` (`limit`, `after_ms`) · 404 |
| GET | `/runs/{run_id}/equity` | `EquityPage` (`limit`, `after_ms`) · 404 |

Nombres de ruta únicos con prefijo `vectorbt_*` (RF-1 de `finances-api`: `url_for` resuelve por nombre). `poll_url`/`result_url` se construyen con `request.url_for("vectorbt_status")` etc., de modo que apunten a `/backtest/vectorbt/...` al montarse.

## 3. Lógica Principal

### 3.1 `engine.py` — frontera RFC-0004
Firmas (cuerpos a escribir con TDD; los nombres de la API de VectorBT se verifican en V1.x, no se asumen):
```python
def to_framework_input(candles: pl.DataFrame, signals: Signals) -> FrameworkInput:
    """PUNTO DE CONVERSIÓN 1: Float32 -> doble precisión (ampliación exacta) + índice posicional;
    entries/exits Boolean de Polars -> arreglos booleanos (sin pérdida)."""

def simulate(inp: FrameworkInput, costs: CostModel,
             size_fraction: float, initial_cash: float) -> FrameworkResult:
    """Desplaza señales (t -> t+1), descarta entradas en la última barra, ejecuta a open, fuerza la salida final."""

def from_framework_output(res: FrameworkResult, open_time_ms: pl.Series) -> EngineOutput:
    """PUNTO DE CONVERSIÓN 2: doble precisión -> Float32/Int64/Int8 con range-check (Art. 5.3, 13)."""

class VectorbtEngine:                       # implementa BacktestEngine; sin supported_kinds ni KINDS_IMPL
    name = "vectorbt"
    def version(self) -> str: ...
    def run(self, candles, signals, costs, size_fraction, initial_cash) -> EngineOutput:
        inp = to_framework_input(candles, signals)
        res = simulate(inp, costs, size_fraction, initial_cash)
        return from_framework_output(res, candles["open_time_ms"])
```
`engine.py` contiene exactamente `to_framework_input`, `simulate`, `from_framework_output` y `VectorbtEngine`. `run` es el único flujo y no sabe qué estrategia produjo las señales. `simulate` opera solo sobre objetos creados en el punto 1. Los timestamps nunca entran al framework (índice posicional); se reasignan desde la `Series` `Int64` original por posición.

### 3.2 Señales: no se calculan aquí
Las señales (SMA, RSI de Wilder, canales de Donchian y cualquier estrategia local) las calcula el núcleo en Polars `Float32` (`finances_core.backtest.strategies`, `specs/finances_backtester/plan.md` §1.3 y §3.7), con la semántica normativa de `finances_backtester` RF-2 y sus tests de calentamiento, cruces estrictos y anti look-ahead. `ft_vectorbt` recibe `Signals` normalizadas (booleanas, calentamiento ya en `false`, sin `entry`+`exit` simultáneas) y solo traduce su ejecución al modelo de RF-5. No hay indicadores de VectorBT en el motor, de modo que ya no existe el riesgo de que su RSI o sus medias difieran de la definición normativa (ADR-V06 sustituido).

### 3.3 Simulación con VectorBT
Verificado en V1.2 con `vectorbt` 1.2.0. `simulate` llama a `vectorbt.Portfolio.from_signals(close, entries, exits, price=…, fees=fee_rate, slippage=s, size=size_fraction, size_type="percent", init_cash=initial_cash, direction="longonly", accumulate=False)` sobre arreglos NumPy (índice posicional) y lee `portfolio.trades.values` (registros de operaciones de salida: `entry_idx`, `exit_idx`, `size`, `entry_price`, `entry_fees`, `exit_price`, `exit_fees`, `pnl`, `return`, `direction`, `status`) y `portfolio.value()` (equity a mercado: `caja + cantidad·close`).

| RF-5 | Semántica de VectorBT 1.2.0 (verificada) | Ajuste en `simulate` |
|---|---|---|
| 1. `t → t+1`, entrada en la última barra ignorada | `from_signals` ejecuta la señal en su misma barra | Señales desplazadas una barra; `entries[n-1] = False` |
| 2. Sin cortos ni acumulación; salida = toda la posición | `direction="longonly"`, `accumulate=False`: una entrada con posición abierta y una salida sin posición se ignoran; la salida cierra toda la posición (`SizeType.Amount = posición`) aunque `size` sea una fracción | Ninguno |
| 3. Precio con deslizamiento | `buy_nb`/`sell_nb`: `price·(1 ± slippage)` | Ninguno |
| 4. Comisión `fee_rate·precio·cantidad` | `req_fees = req_cash·fees` | Ninguno |
| 5. Tamaño `f·caja / (precio·(1 + fee))` | `SizeType.Percent` con `size = f`: `cash_limit = f·caja` y tamaño infinito → rama de caja insuficiente de `buy_nb`: `max_req_cash = cash_limit / (1 + fees)`, `size = max_req_cash / adj_price`, comisión `= cash_limit - max_req_cash` | **Ninguno**: coincide con la fórmula normativa (comprobado a mano: `0,5·100/(11,0055·1,001) = 4,538644`, y en los casos de `test_engine_simulate.py`) |
| 6. Liquidación final al cierre | VectorBT deja la operación abierta (`status = 0`) y la valora a `close` | Si la última barra no tiene salida por señal, se fuerza `exits[n-1] = True` con `price[n-1] = close[n-1]` (mismo deslizamiento de venta); esas operaciones llevan `exit_reason = 1`. Si quedara alguna abierta → `output_out_of_range` |
| 7. `equity` a mercado | `value()` = caja + cantidad·close; en la última barra, tras la liquidación, solo caja (convención de la referencia) | Ninguno |
| RF-6 `return` | El `return` de VectorBT es `pnl / (entry_price·size)` (sin la comisión de entrada) | Se recalcula `pnl / (entry_price·size + entry_fees)`; `fees = entry_fees + exit_fees`; `drawdown = equity / max(caja inicial, máximo acumulado) - 1` |

Evidencia (V1.5): con estos ajustes, `trades` y `equity` del motor sobre el *fixture* coinciden **bit a bit** (diferencia 0 tras el *downcast* a `Float32`) con los *golden* de `sma_cross` (55 operaciones), `rsi_reversion` (11, una `end_of_data`) y `donchian_breakout` (33) y con la referencia de la estrategia sintética (79); el resumen recalculado con `metrics.compute` difiere como máximo 6,25e-6 relativo (`sharpe`), frente a 1e-3 de RFC-0004 punto 5.

### 3.4 `service.py`
`run_sync(request, settings, on_phase)`: instancia `VectorbtEngine`, llama a `run_backtest` (que obtiene el registro de estrategias del núcleo, calcula y normaliza las señales y las pasa al motor) y devuelve `BacktestTaskResult`. `dispatch(request, settings)` crea la tarea (`TaskRecorder` de `finances_core.marketdata.tasks_lifecycle` con `phase_order=("LOADING","SIGNALS","SIMULATING","PERSISTING")`; si resulta no ser genérico, V2.2 lo extrae a `finances_core.backtest.dispatch` sin cambiar a los tres consumidores existentes) y la ejecuta con `asyncio.to_thread` bajo un `asyncio.Semaphore(FT_BACKTEST_MAX_CONCURRENT)`.

`kinds(settings)` y `check_strategies(kind, settings)` son pasos delgados sobre
`strategy_source.kinds` / `strategy_source.check_contract` (Deriva §2.1): la CLI (`kinds`,
`kinds --check`) y la API (`GET /kinds`) comparten el mismo acceso al registro sin importar
`catalog`/`strategies` (RF-21).

### 3.5 Aislamiento (T0.3 análogo a `ft_ta_lib`)
`tests/ft_vectorbt/test_isolation.py` (AST): `FORBIDDEN_ROOTS = ("finances_downloader", "finances_analytics", "finances_api")`, cualquier raíz `ft_*` y hermanas `finances_backtester.ft_*`; ningún import de `finances_core.backtest.strategies` ni `finances_core.backtest.catalog` en el motor (RF-21); `pandas`, `numpy` y `vectorbt` solo en `engine.py`; nombres de doble precisión solo en `engine.py`; y `test_engine_boundary.py` comprueba que la ampliación solo aparece en `to_framework_input` y el *downcast* solo en `from_framework_output`.

## 4. Empaquetado y CI

- **Dependencias del motor** (añadidas a `packages/finances-backtester/pyproject.toml` en V0.2): `vectorbt>=1.2.0,<1.3` (verificado en V0.2), `numpy>=2.5,<3` y `pandas>=3.1,<4` explícitos (los usa `engine.py`); `requires-python = ">=3.11,<3.15"` (A16). La evidencia (`pip list` en Windows y Ubuntu, Python 3.12/3.13/3.14) está en V0.2. **Licencia**: Apache 2.0 con *Commons Clause* v1.0 (prohíbe vender `vectorbt` o un servicio basado sustancialmente en él); el Tech Lead decidió uso no comercial (03/11/2026), anotado en el README.
- Script: `ft_vectorbt = "finances_backtester.ft_vectorbt.cli:app"`; `[tool.setuptools.packages.find] include = ["finances_backtester*"]`.
- `mypy`: `vectorbt`, `numba`, `pandas` como `ignore_missing_imports`/`follow_imports = "skip"` salvo que traigan tipos; `engine.py` con `PD`/`NPY` de ruff deshabilitados por archivo (RFC-0004).
- **CI**: añadir `python -m pip install -e "packages/finances-backtester[dev]"` entre `finances-analytics` y `finances-api`, `finances-backtester` al bucle `for pkg in …`, y un paso `Smoke vectorbt import` análogo a «TA-Lib binary wheel available» (`python -c "import vectorbt; print(vectorbt.__version__)"`). Benchmark `-m benchmark` de `ft_vectorbt` solo en `ubuntu-24.04` con `BENCH_ENFORCE=1` (RF-12), tras calibrar.
- **`finances-api`**: añade `finances-backtester` a `dependencies` (peso: arrastra VectorBT/Numba; ver riesgos) y monta `finances_backtester.ft_vectorbt.api.router` en `create_app()`.

## 5. Estrategia de Tests (Art. 7)
Tests en `packages/finances-backtester/tests/ft_vectorbt/`, sin red, `FT_PROJECT_ROOT` temporal:
- Unitarios: conversiones (ida y vuelta idéntica; dominio y finitud; señales booleanas sin pérdida), `simulate` (desplazamiento, última barra, comisiones, deslizamiento, liquidación final) con casos de 5–10 barras a mano.
- Agnosticismo: `test_engine_signals_agnostic.py` ejecuta el motor con señales arbitrarias de la estrategia sintética de la suite (RF-21) sin ningún `kind`; la firma de `run` no tiene `kind`/`params`.
- Contrato: `engine_contract.assert_engine_matches_golden(VectorbtEngine(), kind)` para las 3 estrategias integradas (las señales las calcula el núcleo y se verifican en `finances-core`, B6.3).
- Servicio: caché, `force`, determinismo byte a byte (dos ejecuciones), concurrencia del mismo `run_id`, errores RF-9.
- CLI (`CliRunner`) y API (`httpx.AsyncClient`): 202 con `task_id`/`poll_url`/`result_url`, *polling* a `COMPLETED`, `409`/`404`, paginación; contrato CLI↔API.
- Benchmark RF-12 (`@pytest.mark.benchmark`).

## 6. Decisiones de Arquitectura (ADR)
| ID | Decisión | Razón |
|----|----------|-------|
| ADR-V01 | Adaptador delgado sobre `run_backtest` de core | Un solo pipeline para todos los motores |
| ADR-V02 | `engine.py` único con dos puntos de conversión (RFC-0004) | Excepción acotada y verificable por AST |
| ADR-V03 | ~~Señales implementadas en `engine.py`~~ **Sustituido por ADR-B09**: las señales se calculan en el núcleo con Polars `Float32`; `engine.py` solo simula (`to_framework_input`, `simulate`, `from_framework_output`) | Añadir estrategias o motores no multiplica el código; una sola semántica y un solo test anti look-ahead; `engine.py` queda mínimo (menos `pandas` que vigilar) |
| ADR-V04 | Índice posicional, tiempos `int64` fuera del framework | Sin `datetime` ni zonas horarias (Art. 5.4) |
| ADR-V05 | Métricas de core, no `Portfolio.stats()` | Comparabilidad entre motores (ADR-B03) |
| ADR-V06 | ~~RSI y medias propios cuando difieran de los de la biblioteca~~ **Obsoleto por ADR-B09**: el motor no calcula indicadores | La semántica la fija el núcleo (RF-2 de `finances_backtester`) |
| ADR-V07 | `TaskRecorder` compartido con fases propias | Reutiliza el mismo contrato de `polling` de la suite |

## 7. Riesgos y Verificaciones Pendientes
- **Compatibilidad VectorBT/Numba/NumPy/pandas**: resuelta en V0.2 (`vectorbt` 1.2.0 funciona en Python 3.12–3.14, Windows y Ubuntu, también en la `.venv` local con 3.14). Riesgo residual: una versión menor nueva de NumPy/pandas/Numba puede romper `vectorbt`; el pin `<1.3` y `vectorbt-probe` acotan ese riesgo.
- **Licencia de `vectorbt`** (Commons Clause): impide un uso comercial de `ft_vectorbt`; la mitigación es la neutralidad de motor (ADR-B09): las estrategias no dependen de `vectorbt`.
- **Mantenimiento de VectorBT** (versión libre vs. PRO): V0.2 registra versión y licencia; si la versión fijable no cumple, el motor se reevalúa (decisión del Tech Lead).
- **Semántica de comisiones y de la operación abierta final**: V1.2 la contrasta con los *golden*.
- **Coste de convertir señales**: copia lineal de dos arreglos booleanos por corrida; despreciable frente a la simulación, medida en el benchmark de V7.1.
- **Determinismo con Numba (hilos, caché JIT)**: V6.2.
- **Peso de la instalación** (Numba/Plotly/Matplotlib transitivos): vigilar el tiempo de CI; evaluar un extra opcional si crece (decisión abierta, no bloquea).
