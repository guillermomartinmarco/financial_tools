# Plan — `finances-backtester` (contratos compartidos, MVP v1.0)

## 1. Modelo de Datos y Contratos

### 1.1 Estructura de módulos
Todo lo compartido vive en `finances-core` (Art. 4); `packages/finances-backtester` aloja solo los motores.

| Responsabilidad | Módulo | RF |
|---|---|---|
| Esquemas Pydantic de petición, estrategia resuelta, costes, manifiesto, resumen, listados, despacho | `finances_core/schemas/backtest.py` | 1, 3, 5, 7, 9 |
| Catálogo de tipos: modelos de parámetros, `CATALOG_VERSION`, `parse_strategy_parameters` | `finances_core/backtest/catalog.py` | 1, 2 |
| Lectura de la estrategia (envoltorio de `ft_strategy_manager`) | `finances_core/backtest/strategy_source.py` | 3 |
| Lector de velas con rango | `finances_core/backtest/candles.py` | 4 |
| Costes y validación de resultados canónicos (`trades`/`equity`) | `finances_core/backtest/costs.py`, `finances_core/backtest/frames.py` | 5, 6 |
| Métricas | `finances_core/backtest/metrics.py` | 7 |
| Identidad (`run_id`, `data_hash`) | `finances_core/backtest/identity.py` | 8 |
| Almacén de resultados (escritura atómica, listado, paginación) | `finances_core/backtest/results_store.py` | 9 |
| Contrato `BacktestEngine`, `EngineOutput` y orquestación `run_backtest` | `finances_core/backtest/engine_api.py`, `finances_core/backtest/runner.py` | 10 |
| Motores | `packages/finances-backtester/src/finances_backtester/ft_<motor>/` | — |
| Fixture, referencia de prueba y suite de contrato | `packages/finances-backtester/tests/contract/` | 10 |

### 1.2 Esquemas (`finances_core/schemas/backtest.py`)
```python
Kind = str                                   # validado contra el catálogo (RF-1)

class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: DatasetIdentifier
    strategy_id: str                         # UUID v7
    strategy_version: int | None = Field(default=None, ge=1)
    start_ms: int | None = None              # Int64 ms UTC
    end_ms: int | None = None
    initial_cash: float = Field(default=10_000.0, gt=0)        # validación Pydantic; en el contrato se almacena Float32
    size_fraction: float = Field(default=1.0, gt=0, le=1)
    fees_bps: float = Field(default=10.0, ge=0)
    slippage_bps: float = Field(default=0.0, ge=0)
    force: bool = False                      # model_validator: start_ms <= end_ms

class ResolvedStrategy(BaseModel):
    strategy_id: str; name: str; version: int; hash: str     # SHA-256 de ft_strategy_manager
    kind: Kind; params: dict[str, int | float]               # ya validados por el modelo del tipo

class CostModel(BaseModel):                  # fee_rate / slip derivados: bps / 10_000
    fees_bps: float; slippage_bps: float

class RunManifest(BaseModel):                # manifest.json (RF-9)
    run_id: str; engine: str; engine_version: str; catalog_version: str; result_schema_version: str
    request: BacktestRequest; strategy: ResolvedStrategy
    start_ms: int; end_ms: int; n_bars: int; data_hash: str; data_gaps: int
    created_at_ms: int; duration_ms: int; warnings: list[str]
    python_version: str; libraries: dict[str, str]

class RunSummary(BaseModel):                 # summary.json (RF-7)
    total_return: float; cagr: float; max_drawdown: float
    sharpe: float | None; sortino: float | None; win_rate: float | None
    n_trades: int; exposure: float; initial_cash: float; final_equity: float; n_bars: int

class BacktestDispatchResponse(BaseModel):   # 202 (Art. 18)
    task_id: str; status: Literal["ACCEPTED"] = "ACCEPTED"; poll_url: str; result_url: str

class BacktestTaskResult(BaseModel):         # GET /result/{task_id}
    run_id: str; cached: bool; summary: RunSummary; manifest: RunManifest
class RunListItem(BaseModel): run_id: str; engine: str; strategy_id: str; strategy_version: int; kind: str; dataset: DatasetIdentifier; created_at_ms: int; summary: RunSummary
class RunsPage(BaseModel): items: list[RunListItem]; count: int; next_cursor: str | None
class TradesPage(BaseModel): items: list[dict[str, Any]]; count: int; next_after_ms: int | None
class EquityPage(BaseModel): items: list[dict[str, Any]]; count: int; next_after_ms: int | None
```
`RunSummary` y las páginas se serializan desde valores `Float32`; los campos del contrato JSON son números. Los dtypes de las tablas están fijados en `frames.py`.

### 1.3 Catálogo (`backtest/catalog.py`)
```python
CATALOG_VERSION = "1.0.0"
RESULT_SCHEMA_VERSION = "1"

class SmaCrossParams(BaseModel):   fast: int = Field(ge=2, le=10_000); slow: int = Field(ge=3, le=10_000)   # validador: fast < slow
class RsiReversionParams(BaseModel): period: int = Field(ge=2, le=1000); lower: float; upper: float          # 0 < lower < upper < 100
class DonchianBreakoutParams(BaseModel): window: int = Field(ge=2, le=10_000)

@dataclass(frozen=True)
class KindSpec:
    kind: str; params_model: type[BaseModel]; description: str
    warmup: Callable[[BaseModel], int]       # primer índice t con señal posible (RF-2): slow / period+1 / window

KINDS: dict[str, KindSpec]                   # único registro; los motores declaran su subconjunto
def parse_strategy_parameters(parameters: dict[str, Any]) -> tuple[Kind, BaseModel]   # UnknownKindError / InvalidParamsError
```
Todos los modelos usan `extra="forbid"`. El catálogo no calcula señales (cada motor las implementa; la semántica normativa es RF-2 y los *golden*).

### 1.4 Contrato de motor (`backtest/engine_api.py`)
```python
@dataclass(frozen=True)
class EngineOutput:
    trades: pl.DataFrame        # RF-6, dtypes canónicos
    equity: pl.DataFrame        # RF-6
    warnings: list[str]

class BacktestEngine(Protocol):
    name: str
    supported_kinds: frozenset[str]
    def version(self) -> str: ...
    def run(self, candles: pl.DataFrame, strategy: ResolvedStrategy,
            costs: CostModel, size_fraction: float, initial_cash: float) -> EngineOutput: ...
```
`frames.validate_engine_output(out, n_bars)` comprueba columnas, dtypes (`Float32`/`Int64`/`Int8`), dominios (`side ∈ {1}`, `exit_reason ∈ {0,1}`), finitud, orden y `equity.height == n_bars` (`OutputOutOfRangeError` si no).

### 1.5 Layout de resultados
```
{FT_RESULTS_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/{run_id}/
  trades.parquet   equity.parquet   summary.json   manifest.json   # manifest.json se escribe el último
```
`list_runs` recorre `*/market_type=*/symbol=*/timeframe=*/*/manifest.json` y filtra; `find_run(run_id)` hace un `glob` por `run_id` (documentado como O(nº de series); suficiente en MVP).

## 2. Mapeo de Interfaces
El paquete de contratos no expone CLI ni API propias. Convención que cada motor `ft_<motor>` replica (se extraerá a una fábrica cuando haya un segundo motor, A20):

| Operación | CLI `ft_<motor>` | API `/backtest/<motor>` |
|---|---|---|
| Lanzar | `run` (síncrono con barra Rich) | `POST /run` → 202 `BacktestDispatchResponse` |
| Estado / resultado de tarea | `status`, `result` (por `task_id`) | `GET /status/{task_id}`, `GET /result/{task_id}` |
| Tipos del catálogo | `kinds` | `GET /kinds` |
| Corridas almacenadas | `runs`, `show RUN_ID`, `trades RUN_ID`, `equity RUN_ID` | `GET /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/trades`, `GET /runs/{run_id}/equity` |

## 3. Lógica Principal

### 3.1 Pipeline `run_backtest` (RF-3..RF-10)
1. `strategy_source.load(strategy_id, version)` → `ResolvedStrategy` (`parse_strategy_parameters`); falla → 404/422.
2. Comprobar `kind ∈ engine.supported_kinds` (RF-1).
3. `candles.read_candles(...)` + `identity.data_hash(candles)` + `run_id`.
4. Si existe resultado y no `force` → devolver (`cached`).
5. `engine.run(...)` en hilo (Art. 3) → `validate_engine_output` → `metrics.compute(...)`.
6. `results_store.write_run(...)` atómico → `BacktestTaskResult`.
Fases para `TaskRecorder`: `PENDING → LOADING → SIMULATING → PERSISTING → COMPLETED/FAILED`.

### 3.2 Métricas (`metrics.py`)
Todo en expresiones Polars sobre `Float32` (Art. 8). `r` se calcula con `shift(1)` y `fill_null(initial_cash)`; media y desviación usan los agregados de Polars; el cociente final y `√P` se aplican sobre el valor ya agregado. La precisión de las sumas largas se verifica con un test que compara con una referencia de prueba de mayor precisión (solo en `tests/`) con tolerancia 1e-3 (RFC-0004). `P` usa `timeframe_duration_ms` (la duración de `1M` es aproximada: 30 días; A8).

### 3.3 Identidad (`identity.py`)
`data_hash` = SHA-256 de los bytes de `open_time_ms`, `open`, `high`, `low`, `close` del rango cargado, en ese orden y en little-endian. `run_id` = SHA-256 del JSON canónico (`json.dumps(sort_keys=True, separators=(",", ":"))`) del diccionario de RF-8. Función pura, probada con vectores fijos.

### 3.4 Almacén (`results_store.py`)
`write_run` escribe en `{run_id}.tmp-{uuid}`, `fsync` best-effort, `os.replace` del directorio completo (si el destino existe y `force`, se renombra el viejo a `.old-{uuid}`, se instala el nuevo y se borra el viejo; si existe y no `force`, se descarta el temporal). Los temporales huérfanos con más de 24 h se limpian al listar.

## 4. Empaquetado (`packages/finances-backtester`)
Espejo de `finances-analytics`: layout `src/`, `setuptools`, `py.typed`, ruff (`line-length = 100`), `mypy --strict`, `pytest` con `asyncio_mode = "auto"`.
- Dependencias de runtime comunes: `finances-core`, `polars`, `pyarrow`, `pydantic`, `pydantic-settings`, `typer`, `fastapi`, `structlog`, `rich`, `uuid6`. Las del motor (`vectorbt`, NumPy, pandas) las añade cada herramienta, con pin verificado (`ft_vectorbt` T0.2).
- `[project.optional-dependencies] dev` igual que `finances-analytics` (+ `httpx`, `hypothesis`).
- `[tool.ruff.lint.per-file-ignores]`: `src/finances_backtester/*/engine.py = ["PD", "NPY"]` (PD/NPY necesarios dentro de la frontera), `cli.py`/`api.py` = `["PLR0913", "PLR0917"]`.
- `[[tool.mypy.overrides]]`: módulos del motor sin stubs tratados como `Any` (`follow_imports = "skip"`).
- **CI** (`.github/workflows/ci.yml`): instalación editable en orden `core → downloader → analytics → backtester → api` (`finances-api` dependerá de `finances-backtester`) y añadir `finances-backtester` al bucle `for pkg in …` de mypy/pytest. Matriz sin cambios (`windows-latest`, `ubuntu-24.04`, Python 3.12) hasta que se pruebe otra.

## 5. Estrategia de Tests (Art. 7)
- `packages/finances-core/tests/unit/test_backtest_*.py`: catálogo, costes/`frames`, métricas (vectores de mano), identidad, almacén (atómico, concurrente, huérfanos), `strategy_source`, `candles`, `runner` con un motor *fake*.
- `packages/finances-core/tests/unit/test_backtest_isolation.py` (AST): `finances_core/backtest/**` y `schemas/backtest.py` sin `pandas`, sin `float64`/`Float64`/`double`, sin imports de `ft_*` salvo `ft_strategy_manager` en `strategy_source.py`.
- `packages/finances-backtester/tests/contract/`: fixture determinista (`PCG64`, semilla fija), referencia de prueba independiente (bucle explícito; tipos de doble precisión permitidos **solo en `tests/`**), *golden* versionados (`signals_*.json`, `trades_*.parquet`, `equity_*.parquet`, `summary_*.json`) y `engine_contract.py` con la suite parametrizable por motor. Un script `tests/contract/regenerate_golden.py` los regenera y exige revisión en el PR.

## 6. Decisiones de Arquitectura (ADR)

| ID | Decisión | Razón |
|----|----------|-------|
| ADR-B01 | Contratos, runner, métricas y almacén en `finances_core` | Art. 4; un único pipeline para todos los motores; los motores solo aportan simulación |
| ADR-B02 | `strategy_source` envuelve `ft_strategy_manager.service.get_strategy` | `ft_strategy_manager` está en `finances-core` (no es herramienta hermana en otro paquete), pero los tests AST de toda herramienta prohíben imports de raíz `ft_*`; el envoltorio vive en `finances_core` y no abre una excepción nueva |
| ADR-B03 | Métricas calculadas en core desde series canónicas | Comparabilidad entre motores; evita `float64` fuera de `engine.py` (RFC-0004) |
| ADR-B04 | Catálogo cerrado; señales implementadas por motor, semántica normativa en la spec y *golden* | Sin DSL ni *plugins*; la validación cruzada detecta divergencias |
| ADR-B05 | `run_id` determinista + manifiesto como marcador de completitud | Idempotencia (Art. 12) y lecturas seguras ante escrituras parciales |
| ADR-B06 | Ejecución a apertura de `t+1` y liquidación final al cierre | Sin anticipación; regla simple de portar a cualquier motor |
| ADR-B07 | *Golden files* de una referencia de prueba independiente | Permite validar el primer motor sin segundo motor; el segundo se valida contra los mismos *golden* |
| ADR-B08 | Resultados compartidos en `FT_RESULTS_DIR` con `engine` en el manifiesto | Un solo almacén y una sola API de lectura para todos los motores |

## 7. Riesgos
- La `.venv` local (Python 3.14, pandas 3.x, NumPy 2.5) puede no ser compatible con VectorBT/Numba: el desarrollo del motor requiere un entorno 3.12 (A16).
- Semántica de comisión/tamaño de cada biblioteca (p. ej. cómo reduce el tamaño para cubrir comisiones): se verifica en la suite de contrato, no se asume.
- Hash de datos en cada petición: coste lineal en el rango; aceptable frente a la simulación.
