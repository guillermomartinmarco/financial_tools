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
tests/ft_vectorbt/ # test_isolation.py, test_engine_boundary.py, test_engine_precision.py, test_kinds_*.py,
                   # test_service.py, test_cli.py, test_api.py, test_contract.py, test_contract_golden.py, benchmarks/
tests/contract/    # fixture, referencia de prueba, golden y engine_contract.py (specs/finances_backtester plan §5)
```
Esquemas, catálogo, contrato de motor, runner, métricas, identidad y almacén: `finances_core.backtest` / `schemas.backtest` (`specs/finances_backtester/plan.md` §1). Esta herramienta no define esquemas propios.

### 1.2 Configuración
Sin variables nuevas (RF-11). `ENGINE_NAME = "vectorbt"`; `engine_version = f"vectorbt {vectorbt.__version__}"`.

## 2. Mapeo de Interfaces

### 2.1 CLI (`ft_vectorbt`)
| Comando | Argumentos principales | Salida / exit |
|---|---|---|
| `run` | `-d ex/mt/sym/tf`, `--strategy-id`, `--strategy-version`, `--start-ms`, `--end-ms`, `--initial-cash`, `--size-fraction`, `--fees-bps`, `--slippage-bps`, `--force`, `--request-file` | resumen (tabla o JSON), `run_id`, `cached`; exit RF-9 |
| `kinds` | — | tipos y JSON Schema de parámetros |
| `runs` | filtros, `--limit`, `--cursor` | listado paginado |
| `show` / `trades` / `equity` | `RUN_ID` (+ `--limit`, `--after-ms`) | manifiesto+resumen / páginas |
| `status` / `result` | `TASK_ID` | estado / `BacktestTaskResult` |

### 2.2 API (prefijo `/backtest/vectorbt`)
| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/run` | `202` `BacktestDispatchResponse` · 400 · 404 · 422 |
| GET | `/status/{task_id}` | estado y fase · 404 |
| GET | `/result/{task_id}` | `BacktestTaskResult` · 404 · 409 · 500 |
| GET | `/kinds` | catálogo |
| GET | `/runs` | `RunsPage` (`exchange`, `market_type`, `symbol`, `timeframe`, `strategy_id`, `limit`, `cursor`) |
| GET | `/runs/{run_id}` | manifiesto + resumen · 404 |
| GET | `/runs/{run_id}/trades` | `TradesPage` (`limit`, `after_ms`) · 404 |
| GET | `/runs/{run_id}/equity` | `EquityPage` (`limit`, `after_ms`) · 404 |

Nombres de ruta únicos con prefijo `vectorbt_*` (RF-1 de `finances-api`: `url_for` resuelve por nombre). `poll_url`/`result_url` se construyen con `request.url_for("vectorbt_status")` etc., de modo que apunten a `/backtest/vectorbt/...` al montarse.

## 3. Lógica Principal

### 3.1 `engine.py` — frontera RFC-0004
Firmas (cuerpos a escribir con TDD; los nombres de la API de VectorBT se verifican en V1.x, no se asumen):
```python
def to_framework_input(candles: pl.DataFrame) -> FrameworkInput:
    """PUNTO DE CONVERSIÓN 1: Float32 -> doble precisión (ampliación exacta) + índice posicional."""

def simulate(inp: FrameworkInput, entries: Bools, exits: Bools, costs: CostModel,
             size_fraction: float, initial_cash: float) -> FrameworkResult:
    """Desplaza señales (t -> t+1), descarta entradas en la última barra, ejecuta a open, fuerza la salida final."""

def from_framework_output(res: FrameworkResult, open_time_ms: pl.Series) -> EngineOutput:
    """PUNTO DE CONVERSIÓN 2: doble precisión -> Float32/Int64/Int8 con range-check (Art. 5.3, 13)."""

class VectorbtEngine:                       # implementa BacktestEngine
    name = "vectorbt"; supported_kinds = frozenset(KINDS_IMPL)
    def version(self) -> str: ...
    def run(self, candles, strategy, costs, size_fraction, initial_cash) -> EngineOutput:
        inp = to_framework_input(candles)
        entries, exits = KINDS_IMPL[strategy.kind](inp, strategy.params)
        res = simulate(inp, entries, exits, costs, size_fraction, initial_cash)
        return from_framework_output(res, candles["open_time_ms"])

KINDS_IMPL: dict[str, Callable[[FrameworkInput, dict[str, int | float]], tuple[Bools, Bools]]]
```
`run` es el único flujo: `to_framework_input` → señales del tipo → `simulate` → `from_framework_output`. `simulate` y las señales operan solo sobre objetos creados en el punto 1. Los timestamps nunca entran al framework (índice posicional); se reasignan desde la `Series` `Int64` original por posición.

### 3.2 Señales por tipo (en `engine.py`, semántica normativa = `finances_backtester` RF-2)
- `sma_cross`: medias móviles simples con ventana exacta (`t ≥ n-1`), cruces con comparaciones estrictas entre `t-1` y `t`; `NaN` de calentamiento → `false`.
- `rsi_reversion`: RSI de Wilder propio (semilla SMA de `period` variaciones, recursión `(prev·(period-1)+x)/period`, casos 100/50 de RF-2); no se usa el RSI por defecto de VectorBT sin comprobar su semilla.
- `donchian_breakout`: máximo/mínimo móviles de `window` barras **desplazados una barra** (excluyen la actual).
Cada función devuelve `(entries, exits)` ya enmascarados por el calentamiento de `KindSpec.warmup`.

### 3.3 Simulación con VectorBT
Candidato a verificar en V1.2: `vectorbt.Portfolio.from_signals` con `price` = apertura, `fees`, `slippage`, solo largo, `size` como fracción de la caja, sin acumulación; salida de operaciones (`trades.records_readable` o equivalente) y valor de cartera (`value()`) para construir `trades`/`equity` canónicos. Si la semántica de comisiones/tamaño o el cierre de la posición final no coincide con RF-5, `simulate` ajusta (tamaño explícito por orden, precio de la última barra = cierre con salida forzada) y los *golden* lo demuestran. La equity que no sea de valor a mercado al cierre se reconstruye desde caja y cantidad.

### 3.4 `service.py`
`run_sync(request, settings, on_phase)`: instancia `VectorbtEngine`, llama a `run_backtest` y devuelve `BacktestTaskResult`. `dispatch(request, settings)` crea la tarea (`TaskRecorder` de `finances_core.marketdata.tasks_lifecycle` con `phase_order=("LOADING","SIMULATING","PERSISTING")`; si resulta no ser genérico, V2.2 lo extrae a `finances_core.backtest.dispatch` sin cambiar a los tres consumidores existentes) y la ejecuta con `asyncio.to_thread` bajo un `asyncio.Semaphore(FT_BACKTEST_MAX_CONCURRENT)`.

### 3.5 Aislamiento (T0.3 análogo a `ft_ta_lib`)
`tests/ft_vectorbt/test_isolation.py` (AST): `FORBIDDEN_ROOTS = ("finances_downloader", "finances_analytics", "finances_api")`, cualquier raíz `ft_*` y hermanas `finances_backtester.ft_*`; `pandas`, `numpy` y `vectorbt` solo en `engine.py`; nombres de doble precisión solo en `engine.py`; y `test_engine_boundary.py` comprueba que la ampliación solo aparece en `to_framework_input` y el *downcast* solo en `from_framework_output`.

## 4. Empaquetado y CI

- **Dependencias del motor** (añadidas a `packages/finances-backtester/pyproject.toml` en V0.2): `vectorbt` — **pin a verificar en V0.2** (no se inventa versión) —, `numpy` y `pandas` con los límites que exija la versión resuelta de `vectorbt`/`numba`; `requires-python` acotado según evidencia (A16). La evidencia (salida de `pip install` y `pip list` en Windows y Ubuntu, Python 3.12) se registra en V0.2.
- Script: `ft_vectorbt = "finances_backtester.ft_vectorbt.cli:app"`; `[tool.setuptools.packages.find] include = ["finances_backtester*"]`.
- `mypy`: `vectorbt`, `numba`, `pandas` como `ignore_missing_imports`/`follow_imports = "skip"` salvo que traigan tipos; `engine.py` con `PD`/`NPY` de ruff deshabilitados por archivo (RFC-0004).
- **CI**: añadir `python -m pip install -e "packages/finances-backtester[dev]"` entre `finances-analytics` y `finances-api`, `finances-backtester` al bucle `for pkg in …`, y un paso `Smoke vectorbt import` análogo a «TA-Lib binary wheel available» (`python -c "import vectorbt; print(vectorbt.__version__)"`). Benchmark `-m benchmark` de `ft_vectorbt` solo en `ubuntu-24.04` con `BENCH_ENFORCE=1` (RF-12), tras calibrar.
- **`finances-api`**: añade `finances-backtester` a `dependencies` (peso: arrastra VectorBT/Numba; ver riesgos) y monta `finances_backtester.ft_vectorbt.api.router` en `create_app()`.

## 5. Estrategia de Tests (Art. 7)
Tests en `packages/finances-backtester/tests/ft_vectorbt/`, sin red, `FT_PROJECT_ROOT` temporal:
- Unitarios: conversiones (ida y vuelta idéntica; dominio y finitud), señales de cada tipo contra los `signals_*.json` *golden*, `simulate` (desplazamiento, última barra, comisiones, deslizamiento, liquidación final) con casos de 5–10 barras a mano.
- Contrato: `engine_contract.assert_engine_matches_golden(VectorbtEngine(), kind)` para los 3 tipos.
- Servicio: caché, `force`, determinismo byte a byte (dos ejecuciones), concurrencia del mismo `run_id`, errores RF-9.
- CLI (`CliRunner`) y API (`httpx.AsyncClient`): 202 con `task_id`/`poll_url`/`result_url`, *polling* a `COMPLETED`, `409`/`404`, paginación; contrato CLI↔API.
- Benchmark RF-12 (`@pytest.mark.benchmark`).

## 6. Decisiones de Arquitectura (ADR)
| ID | Decisión | Razón |
|----|----------|-------|
| ADR-V01 | Adaptador delgado sobre `run_backtest` de core | Un solo pipeline para todos los motores |
| ADR-V02 | `engine.py` único con dos puntos de conversión (RFC-0004) | Excepción acotada y verificable por AST |
| ADR-V03 | Señales implementadas en `engine.py` (sin `kinds.py`) | Las señales manipulan estructuras del framework; mantenerlas en la frontera evita fugas de `pandas` |
| ADR-V04 | Índice posicional, tiempos `int64` fuera del framework | Sin `datetime` ni zonas horarias (Art. 5.4) |
| ADR-V05 | Métricas de core, no `Portfolio.stats()` | Comparabilidad entre motores (ADR-B03) |
| ADR-V06 | RSI y medias propios cuando difieran de los de la biblioteca | La semántica del catálogo manda sobre la de la biblioteca |
| ADR-V07 | `TaskRecorder` compartido con fases propias | Reutiliza el mismo contrato de `polling` de la suite |

## 7. Riesgos y Verificaciones Pendientes
- **Compatibilidad VectorBT/Numba/NumPy/pandas con Python 3.12 en Windows y Ubuntu**: no verificada; V0.2 decide pin y cota de `requires-python` con evidencia. La `.venv` local (Python 3.14, pandas 3.x) probablemente no sirva.
- **Mantenimiento de VectorBT** (versión libre vs. PRO): V0.2 registra versión y licencia; si la versión fijable no cumple, el motor se reevalúa (decisión del Tech Lead).
- **Semántica de comisiones y de la operación abierta final**: V1.2 la contrasta con los *golden*.
- **Determinismo con Numba (hilos, caché JIT)**: V6.2.
- **Peso de la instalación** (Numba/Plotly/Matplotlib transitivos): vigilar el tiempo de CI; evaluar un extra opcional si crece (decisión abierta, no bloquea).
