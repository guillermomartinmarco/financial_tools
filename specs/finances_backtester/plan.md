# Plan — `finances-backtester` (contratos compartidos, MVP v1.0)

## 1. Modelo de Datos y Contratos

### 1.1 Estructura de módulos
Todo lo compartido vive en `finances-core` (Art. 4); `packages/finances-backtester` aloja solo los motores.

| Responsabilidad | Módulo | RF |
|---|---|---|
| Esquemas Pydantic de petición, estrategia resuelta, costes, manifiesto, resumen, listados, despacho, `KindInfo` | `finances_core/schemas/backtest.py` | 1, 3, 5, 7, 9 |
| Registro de estrategias: `StrategyRegistry`, `CATALOG_VERSION`, `parse_strategy_parameters`, `get_registry` | `finances_core/backtest/catalog.py` | 1 |
| Contrato `BaseStrategy`, `Signals`, `MetricsTarget`, normalización `finalize` | `finances_core/backtest/strategies/base.py`, `strategies/signals.py` | 2, 16 |
| Descubrimiento y carga (integradas + `FT_STRATEGIES_DIR`), colisiones, errores de inicialización | `finances_core/backtest/strategies/discovery.py` | 18 |
| Guarda AST, estructura de carpeta y secciones del README | `finances_core/backtest/strategies/checks.py` | 11, 17, 18 |
| Hash del código (`strategy_code_hash`) | `finances_core/backtest/strategies/hashing.py` | 19 |
| Verificación de autor: `assert_strategy_contract`, anti look-ahead | `finances_core/backtest/strategies/testing.py` | 20 |
| Ayudas compartidas en Polars `Float32` (`sma`, `rsi_wilder`, `rolling_max_prev`, `cross_above`…) | `finances_core/backtest/strategies/_lib/` | 2 |
| Estrategias integradas (una carpeta cada una) | `finances_core/backtest/strategies/{sma_cross,rsi_reversion,donchian_breakout}/` | 2, 17 |
| Plantilla copiable para autores | `finances_core/backtest/strategies/_template/` | 22 |
| Estrategias locales del usuario | `{FT_STRATEGIES_DIR}/<kind>/` (fuera del repositorio) | 18 |
| Lectura de la estrategia (envoltorio de `ft_strategy_manager`) | `finances_core/backtest/strategy_source.py` | 3 |
| Lector de velas con rango | `finances_core/backtest/candles.py` | 4 |
| Costes y validación de resultados canónicos (`trades`/`equity`) | `finances_core/backtest/costs.py`, `finances_core/backtest/frames.py` | 5, 6 |
| Métricas | `finances_core/backtest/metrics.py` | 7 |
| Identidad (`run_id`, `data_hash`) | `finances_core/backtest/identity.py` | 8 |
| Almacén de resultados (escritura atómica, listado, paginación) | `finances_core/backtest/results_store.py` | 9 |
| Contrato `BacktestEngine`, `EngineOutput` y orquestación `run_backtest` | `finances_core/backtest/engine_api.py`, `finances_core/backtest/runner.py` | 10 |
| Motores (solo simulación) | `packages/finances-backtester/src/finances_backtester/ft_<motor>/` | 21 |
| Fixture, referencia de prueba y suite de contrato | `packages/finances-backtester/tests/contract/` | 10 |
| Guía de autor | `docs/guides/strategy-authoring.md` | 22 |

Árbol de una estrategia (integrada o local):
```
strategies/<kind>/                # <kind> == nombre de carpeta == BaseStrategy.kind
  logic.py                        # obligatorio: una subclase concreta de BaseStrategy
  README.md                       # obligatorio: secciones de RF-17
  <otros>.py                      # opcional: módulos hermanos (entran en strategy_code_hash)
```

### 1.2 Esquemas (`finances_core/schemas/backtest.py`)
```python
Kind = str                                   # validado contra el registro (RF-1)

class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")   # sin campos de código, rutas ni módulos (RF-3, RF-18)
    dataset: DatasetIdentifier
    strategy_id: str                         # UUID v7
    strategy_version: int | None = Field(default=None, ge=1)
    start_ms: int | None = None              # Int64 ms UTC
    end_ms: int | None = None
    initial_cash: float = Field(default=10_000.0, gt=0)        # validación Pydantic; en el contrato se almacena Float32
    size_fraction: float = Field(default=1.0, gt=0, le=1)
    fees_bps: float = Field(default=10.0, ge=0)
    slippage_bps: float = Field(default=0.0, ge=0)
    risk: RiskModel | None = None            # stops SL/TP/trailing a nivel de corrida (ver Enmienda G4)
    force: bool = False                      # model_validator: start_ms <= end_ms

class ResolvedStrategy(BaseModel):
    strategy_id: str; name: str; version: int; hash: str     # SHA-256 de ft_strategy_manager
    kind: Kind; params: dict[str, int | float | bool | str]  # ya validados por Params de la estrategia
    kind_version: str                        # BaseStrategy.version
    code_hash: str                           # strategy_code_hash (RF-19)
    origin: Literal["builtin", "local"]

class KindInfo(BaseModel):                   # GET /kinds, `kinds` (RF-1)
    kind: str; version: str | None; origin: Literal["builtin", "local"]
    description: str | None; code_hash: str | None
    params_schema: dict[str, Any] | None     # Params.model_json_schema(); fuente única, sin schema.json
    default_params: dict[str, Any] | None
    metrics_target: dict[str, str] | None
    valid: bool; error: str | None           # carpeta local inválida (RF-18)

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

### 1.3 Contrato de estrategia y registro (`backtest/strategies/`, `backtest/catalog.py`)
```python
# strategies/base.py  (forma vigente: ver Enmienda L0-G2a y Enmienda G2b)
class Signals(NamedTuple):
    # G2a: las estrategias emiten posición-objetivo (pl.Series Int8) y el núcleo deriva los
    # flancos; G2b: Signals lleva 4 campos (largo + corto).
    entries: pl.Series        # Boolean, len == candles.height; señal al cierre de t (RF-2)
    exits: pl.Series
    entries_short: pl.Series  # Enmienda G2b (corto)
    exits_short: pl.Series

class MetricsTarget(BaseModel):              # informativo (RF-16); sin efecto en la ejecución
    model_config = ConfigDict(extra="forbid")
    metric: Literal["total_return", "cagr", "max_drawdown", "sharpe", "sortino", "win_rate", "exposure"]
    direction: Literal["max", "min"]

P = TypeVar("P", bound=BaseModel)

class BaseStrategy(ABC, Generic[P]):
    kind: ClassVar[str]                       # ^[a-z][a-z0-9_]{2,39}$ == nombre de carpeta
    version: ClassVar[str]                    # SemVer de la lógica
    description: ClassVar[str]
    Params: ClassVar[type[BaseModel]]         # extra="forbid"; fuente única de tipos, rangos y relaciones
    default_params: ClassVar[Mapping[str, int | float | bool | str]]   # DEBE validar contra Params
    metrics_target: ClassVar[MetricsTarget | None] = None

    @staticmethod
    @abstractmethod
    def warmup(params: P) -> int: ...         # primer t con señal posible (RF-2)

    @staticmethod
    @abstractmethod
    def signals(candles: pl.DataFrame, params: P) -> Signals: ...   # pura, Polars Float32, sin I/O ni estado
```
```python
# strategies/signals.py
def finalize(sig: Signals, *, n: int, warmup: int) -> Signals:
    """Valida Boolean y longitud (InvalidSignalsError); nulos y t < warmup -> False; entry&exit simultáneas -> False."""

# catalog.py
CATALOG_VERSION = "1.0.0"     # versiona contrato + infraestructura común + strategies/_lib (A22), no cada tipo
RESULT_SCHEMA_VERSION = "1"

@dataclass(frozen=True)
class StrategyRegistry:
    valid: Mapping[str, type[BaseStrategy]]          # ordenado por kind ascendente
    invalid: Mapping[str, str]                       # kind/carpeta -> mensaje legible (RF-18)
    origins: Mapping[str, Literal["builtin", "local"]]
    code_hashes: Mapping[str, str]                   # strategy_code_hash por kind (RF-19)
    def kinds(self) -> list[KindInfo]: ...           # válidas + inválidas, orden determinista

def get_registry(settings: FinancesSettings) -> StrategyRegistry   # una vez por proceso (RF-18)
def parse_strategy_parameters(parameters: dict[str, Any], registry: StrategyRegistry) -> tuple[type[BaseStrategy], BaseModel]
# UnknownKindError / StrategyInitError / InvalidParamsError
```
`Params` usa `extra="forbid"`. El registro no calcula señales; las calcula el `runner` con `signals.finalize(strategy.signals(candles, params), …)`. Las tres estrategias integradas definen: `sma_cross` (`fast`: `ge=2, le=10_000`; `slow`: `ge=3, le=10_000`; validador `fast < slow`; `warmup = slow`), `rsi_reversion` (`period`: `ge=2, le=1000`; `lower`, `upper` con `0 < lower < upper < 100`; `warmup = period + 1`), `donchian_breakout` (`window`: `ge=2, le=10_000`; `warmup = window`).

### 1.4 Contrato de motor (`backtest/engine_api.py`)
```python
@dataclass(frozen=True)
class EngineOutput:
    trades: pl.DataFrame        # RF-6, dtypes canónicos
    equity: pl.DataFrame        # RF-6
    warnings: list[str]

class BacktestEngine(Protocol):
    name: str
    def version(self) -> str: ...
    def run(self, candles: pl.DataFrame, signals: Signals,
            costs: CostModel, size_fraction: float, initial_cash: float,
            risk: RiskModel | None = None) -> EngineOutput: ...   # risk: ver Enmienda G4 (aditivo)
```
Sin `supported_kinds`, `kind`, `params` ni `ResolvedStrategy` (RF-10, RF-21, ADR-B09). `Signals` ya está normalizado (RF-16). `frames.validate_engine_output(out, n_bars)` comprueba columnas, dtypes (`Float32`/`Int64`/`Int8`), dominios (`side ∈ {1, -1}` tras Enmienda G2b, `exit_reason ∈ {0, 1, 2, 3}` tras Enmienda G4), finitud, orden y `equity.height == n_bars` (`OutputOutOfRangeError` si no).

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
| Estrategias registradas (`KindInfo`) | `kinds [--kind KIND] [--check]` | `GET /kinds` |
| Corridas almacenadas | `runs`, `show RUN_ID`, `trades RUN_ID`, `equity RUN_ID` | `GET /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/trades`, `GET /runs/{run_id}/equity` |

`kinds --check` (solo CLI, A25; adaptador de `check_strategy(kind, policy="local")`, ADR-B13) ejecuta `assert_strategy_contract` (RF-20) sobre las estrategias seleccionadas (todas las locales por defecto) y sale con exit 2 si alguna falla; sin `--check` solo lista el registro (exit 0 aunque haya carpetas inválidas, que se muestran con su error). La API no tiene endpoint alguno que acepte código ni rutas (RF-18).

## 3. Lógica Principal

### 3.1 Pipeline `run_backtest` (RF-3..RF-10)
1. `strategy_source.load(strategy_id, version)` → `ResolvedStrategy` (`parse_strategy_parameters` contra `get_registry`); falla → 404/400/422/`strategy_init_error`.
2. `candles.read_candles(...)` + `identity.data_hash(candles)` + `run_id` (incluye `kind_version` y `strategy_code_hash`).
3. Si existe resultado y no `force` → devolver (`cached`), **sin calcular señales**.
4. Señales: `strategy.signals(candles, params)` en el hilo de trabajo (Art. 3) → `signals.finalize` (`invalid_signals` / `strategy_runtime_error`). Fase `SIGNALS`.
5. `engine.run(candles, signals, costs, …)` en hilo → `validate_engine_output` → `metrics.compute(...)`.
6. `results_store.write_run(...)` atómico → `BacktestTaskResult`.
Fases para `TaskRecorder`: `PENDING → LOADING → SIGNALS → SIMULATING → PERSISTING → COMPLETED/FAILED`.

### 3.2 Métricas (`metrics.py`)
Todo en expresiones Polars sobre `Float32` (Art. 8). `r` se calcula con `shift(1)` y `fill_null(initial_cash)`; media y desviación usan los agregados de Polars; el cociente final y `√P` se aplican sobre el valor ya agregado. La precisión de las sumas largas se verifica con un test que compara con una referencia de prueba de mayor precisión (solo en `tests/`) con tolerancia 1e-3 (RFC-0004). `P` usa `timeframe_duration_ms` (la duración de `1M` es aproximada: 30 días; A8).

### 3.3 Identidad (`identity.py`)
`data_hash` = SHA-256 de los bytes de `open_time_ms`, `open`, `high`, `low`, `close` del rango cargado, en ese orden y en little-endian. `run_id` = SHA-256 del JSON canónico (`json.dumps(sort_keys=True, separators=(",", ":"))`) del diccionario de RF-8 (incluidos `kind_version` y `strategy_code_hash`). Función pura, probada con vectores fijos.

### 3.4 Almacén (`results_store.py`)
`write_run` escribe en `{run_id}.tmp-{uuid}`, `fsync` best-effort, `os.replace` del directorio completo (si el destino existe y `force`, se renombra el viejo a `.old-{uuid}`, se instala el nuevo y se borra el viejo; si existe y no `force`, se descarta el temporal). Los temporales huérfanos con más de 24 h se limpian al listar.

### 3.5 Descubrimiento y carga (`strategies/discovery.py`, RF-18)
1. Enumerar subdirectorios de `finances_core/backtest/strategies/` (integradas) y de `FT_STRATEGIES_DIR` resuelto a ruta absoluta (locales); descartar nombres que empiecen por `_` o `.` y entradas que no sean directorios.
2. Para cada carpeta, en orden de nombre ascendente: `checks.check_structure` (existencia de `logic.py` y `README.md`, secciones de RF-17) → `checks.check_ast` sobre todos los `*.py` (importaciones permitidas, nombres y llamadas prohibidos) → carga con `importlib.util.spec_from_file_location` bajo un paquete sintético `ft_strategy_<origin>_<kind>` (permite imports relativos entre módulos hermanos, sin tocar `sys.path`) → exactamente una subclase concreta de `BaseStrategy` → `kind == carpeta` → `default_params` validan contra `Params` → tabla del README coincide con `Params.model_fields`. Cualquier fallo registra `invalid[kind] = mensaje` (sin traza) y continúa.
3. Colisión: una carpeta local con `kind` ya presente entre las integradas queda `invalid` con `kind_collision`; nunca reemplaza.
4. Una integrada inválida eleva `StrategyInitError` al construir el registro (defecto del paquete).
5. `code_hashes[kind] = hashing.code_hash(carpeta)`; el registro final se ordena por `kind`. Emite el evento `strategies_discovered`.
`get_registry` se cachea por proceso; no hay recarga en caliente (RF-18).

### 3.6 Hash del código (`strategies/hashing.py`, RF-19)
`code_hash(folder)` = SHA-256 de la concatenación, ordenada por ruta POSIX relativa, de `ruta + b"\0" + contenido(CRLF→LF) + b"\0"` para cada `*.py` de la carpeta (recursivo; se excluyen `README.md` y `__pycache__`). Función pura probada con vectores fijos (mismo contenido en Windows y Ubuntu, `README` no influye, añadir un `.py` hermano cambia el hash).

### 3.7 Normalización de señales (`strategies/signals.py`, RF-16)
`finalize`: (1) `entries`/`exits` deben ser `Boolean` y de longitud `n`, o `InvalidSignalsError`; (2) nulos en `t ≥ warmup` → `InvalidSignalsError`; (3) nulos y `t < warmup` → `false`; (4) `both = entries & exits`; `entries &= ~both`; `exits &= ~both`. Se aplica una sola vez en el `runner`; los motores asumen la entrada normalizada.

### 3.8 Verificación de autor (`strategies/testing.py`, `strategies/checks.py`, RF-20)
`assert_strategy_contract(kind, registry)`: estructura + AST + contrato de señales + anti look-ahead. El anti look-ahead usa una serie sintética determinista (`PCG64`, semilla fija, 600 barras `Float32` con tendencia y rango), `default_params` y cualquier `check_params` opcional de la estrategia, y los cortes `{warmup, warmup+1, n/4, n/2, 3n/4, n-1}` (los inválidos se descartan); compara `finalize(signals(candles[:k]))` con `finalize(signals(candles))[:k]` para `entries` y `exits`. Una estrategia de prueba con `shift(-1)` demuestra que el test detecta la anticipación.

## 4. Empaquetado (`packages/finances-backtester`)
Espejo de `finances-analytics`: layout `src/`, `setuptools`, `py.typed`, ruff (`line-length = 100`), `mypy --strict`, `pytest` con `asyncio_mode = "auto"`.
- Dependencias de runtime comunes: `finances-core`, `polars`, `pyarrow`, `pydantic`, `pydantic-settings`, `typer`, `fastapi`, `structlog`, `rich`, `uuid6`. Las del motor (`vectorbt`, NumPy, pandas) las añade cada herramienta, con pin verificado (`ft_vectorbt` V0.2).
- `finances-core` debe empaquetar los `README.md` y la carpeta `_template/` de `strategies/` como *package data* (`pyproject.toml` de `finances-core`) para que el descubrimiento de integradas funcione instalado, no solo en modo editable.
- `[project.optional-dependencies] dev` igual que `finances-analytics` (+ `httpx`, `hypothesis`).
- `[tool.ruff.lint.per-file-ignores]`: `src/finances_backtester/*/engine.py = ["PD", "NPY"]` (PD/NPY necesarios dentro de la frontera), `cli.py`/`api.py` = `["PLR0913", "PLR0917"]`.
- `[[tool.mypy.overrides]]`: módulos del motor sin stubs tratados como `Any` (`follow_imports = "skip"`).
- **CI** (`.github/workflows/ci.yml`): instalación editable en orden `core → downloader → analytics → backtester → api` (`finances-api` dependerá de `finances-backtester`) y añadir `finances-backtester` al bucle `for pkg in …` de mypy/pytest. Matriz sin cambios (`windows-latest`, `ubuntu-24.04`, Python 3.12) hasta que se pruebe otra.

## 5. Estrategia de Tests (Art. 7)
- `packages/finances-core/tests/unit/test_backtest_*.py`: registro, contrato y `finalize`, descubrimiento (orden, colisión, error de carpeta incompleta, integrada inválida), hash del código, costes/`frames`, métricas (vectores de mano), identidad, almacén (atómico, concurrente, huérfanos), `strategy_source`, `candles`, `runner` con un motor *fake*.
- `packages/finances-core/tests/unit/test_strategies_*.py`: una carpeta por integrada con casos de 10 barras a mano, `assert_strategy_contract` (estructura + README + AST + anti look-ahead), estrategia con `shift(-1)` que **debe** fallar, estrategia local temporal (`tmp_path` como `FT_STRATEGIES_DIR`) válida e inválida, `_template` copiada y renombrada que **debe** pasar, y ejemplos de la guía.
- `packages/finances-core/tests/unit/test_backtest_isolation.py` (AST): `finances_core/backtest/**` (incluido `strategies/`) y `schemas/backtest.py` sin `pandas`, sin NumPy, sin `float64`/`Float64`/`double`, sin imports de `ft_*` salvo `ft_strategy_manager` en `strategy_source.py`.
- `packages/finances-backtester/tests/contract/`: fixture determinista (`PCG64`, semilla fija), referencia de prueba independiente (bucle explícito; tipos de doble precisión permitidos **solo en `tests/`**), *golden* versionados (`signals_*.json`, `trades_*.parquet`, `equity_*.parquet`, `summary_*.json`), una estrategia sintética de prueba (RF-21), `test_signals_golden.py` (las señales del núcleo igualan los `signals_*.json`) y `engine_contract.py` con la suite parametrizable por motor. Un script `tests/contract/regenerate_golden.py` los regenera y exige revisión en el PR.

## 6. Decisiones de Arquitectura (ADR)

| ID | Decisión | Razón |
|----|----------|-------|
| ADR-B01 | Contratos, runner, métricas y almacén en `finances_core` | Art. 4; un único pipeline para todos los motores; los motores solo aportan simulación |
| ADR-B02 | `strategy_source` envuelve `ft_strategy_manager.service.get_strategy` | `ft_strategy_manager` está en `finances-core` (no es herramienta hermana en otro paquete), pero los tests AST de toda herramienta prohíben imports de raíz `ft_*`; el envoltorio vive en `finances_core` y no abre una excepción nueva |
| ADR-B03 | Métricas calculadas en core desde series canónicas | Comparabilidad entre motores; evita `float64` fuera de `engine.py` (RFC-0004) |
| ADR-B04 | ~~Catálogo cerrado; señales implementadas por motor~~ **Sustituido por ADR-B09 y ADR-B10** | Ya no hay catálogo cerrado ni señales por motor |
| ADR-B05 | `run_id` determinista + manifiesto como marcador de completitud | Idempotencia (Art. 12) y lecturas seguras ante escrituras parciales |
| ADR-B06 | Ejecución a apertura de `t+1` y liquidación final al cierre | Sin anticipación; regla simple de portar a cualquier motor |
| ADR-B07 | *Golden files* de una referencia de prueba independiente | Permite validar el primer motor sin segundo motor; el segundo se valida contra los mismos *golden* |
| ADR-B08 | Resultados compartidos en `FT_RESULTS_DIR` con `engine` en el manifiesto | Un solo almacén y una sola API de lectura para todos los motores |
| ADR-B09 | **Señales en el núcleo con Polars `Float32`; los motores solo simulan** (sustituye a ADR-B04 y a ADR-V03 de `ft_vectorbt`) | Escalabilidad: N estrategias × M motores pasa de N·M implementaciones a N + M; una sola semántica normativa, un solo test anti look-ahead y *golden* de señales independientes del motor; el motor GPU futuro tiene un oráculo CPU de señales. Ver tabla de compromisos abajo |
| ADR-B10 | **Estrategias Python locales permitidas solo vía CLI/directorio local; nunca código por API** (revierte la prohibición de «plugins, código del usuario o DSL») | La necesidad real es programar estrategias propias; el riesgo es ejecución arbitraria: se acota con la frontera «la API solo ejecuta lo ya instalado», registro sin recarga en caliente, guarda AST, colisiones rechazadas y hash del código en `run_id`. Alternativas descartadas: subida por API (RCE); DSL ahora (alcance mayor, queda para `formulaic_alpha`); sandbox en subproceso (complejidad; A23) |
| ADR-B11 | Contrato `BaseStrategy` con `Params` Pydantic como fuente única, carpeta por estrategia con `logic.py` + `README.md` verificado por test | Un solo esquema (el JSON Schema se genera, no hay `schema.json` duplicado); la documentación (lógica, matemática, diagrama, parámetros) no puede quedar desactualizada porque un test compara la tabla con `Params` |
| ADR-B12 | Reproducibilidad: `strategy_code_hash` (solo `*.py` de la carpeta) + `kind_version` en `run_id`; `CATALOG_VERSION` versiona contrato e infraestructura (incluida `_lib`) | Editar código local invalida resultados cacheados sin versionado manual; `_lib` fuera del hash evita invalidar todo ante un cambio de ayuda, y se cubre con `CATALOG_VERSION` (A22) |
| ADR-B13 | **Chequeo de estrategias como servicio con política de ejecución**: `check_strategy(kind, policy) -> CheckReport` (modelo Pydantic) en el núcleo; la CLI y, más adelante, la API son adaptadores del mismo servicio. `policy = local` (CLI: importa y ejecuta, incluye el test de anticipación) / `static` (API: solo AST, estructura, README y hash; sin importar código) / `isolated` (futuro: subproceso con límites, requiere RFC) | La excepción a la simetría CLI/API (A25) es una **política explícita**, no una funcionalidad ausente: el contrato y el informe son los mismos, solo cambia qué comprobaciones dinámicas se permiten. Cuando exista el aislamiento (A23) se habilita `isolated` en la API sin tocar contratos. Alternativas descartadas: dejar el chequeo solo en la CLI sin servicio (duplica lógica al añadir la API); ejecutarlo en la API en proceso (RCE) |

### Compromisos de ADR-B09 (señales en el núcleo)
| Ventaja | Coste / límite | Mitigación |
|---|---|---|
| Añadir un motor = simulación únicamente (RF-21) | Cada motor convierte `Signals` (booleanos) a su estructura: copia lineal, barata frente a la simulación | La conversión de booleanos no introduce `float64` (RFC-0004) |
| Semántica única y comparable; *golden* de señales una sola vez | El núcleo debe calcular en `Float32`; recursiones (RSI de Wilder) acumulan más error que en doble precisión | *Golden* con referencia de prueba en doble precisión y *fixture* con margen en umbrales (A6) |
| Test anti look-ahead y de estructura una sola vez | Estrategias dependientes del estado de ejecución (stop sobre precio de entrada, tamaño dinámico, piramidación, órdenes límite, intrabarra, cortos, multiactivo) no caben en `(entries, exits)` | Fuera de alcance (spec «No-Go»); extensión futura abajo |
| El motor GPU futuro reutiliza el oráculo de señales | Un barrido de parámetros recalcularía señales en CPU por combinación | `ft_backtestopt_spectre` tendrá su RFC y podrá requerir señales nativas |

**Extensión futura (no implementada ni especificada):** un motor podría declarar *estrategias nativas del motor* (p. ej. un atributo opcional `native_kinds: frozenset[str]` en `BacktestEngine`, por defecto vacío). El núcleo conservaría el registro de metadatos (`kind`, `Params`, README, `version`, `code_hash`) y el motor aportaría la implementación nativa, validada contra el oráculo de Polars cuando exista. Solo entonces podría reaparecer un error del tipo `kind_not_supported_by_engine`, limitado a esas estrategias. Requiere su propio RF/ADR; hasta entonces el registro rechaza declararlas (RF-21).

## 7. Riesgos
- La `.venv` local (Python 3.14, pandas 3.x, NumPy 2.5) puede no ser compatible con VectorBT/Numba: el desarrollo del motor requiere un entorno 3.12 (A16).
- Semántica de comisión/tamaño de cada biblioteca (p. ej. cómo reduce el tamaño para cubrir comisiones): se verifica en la suite de contrato, no se asume.
- Hash de datos en cada petición: coste lineal en el rango; aceptable frente a la simulación.
- **Ejecución de código local en proceso** (A23): quien escribe en `FT_STRATEGIES_DIR` ejecuta código con los privilegios del servicio; la guarda AST es evadible. Mitigación: la API nunca acepta código; permisos del directorio documentados en la guía; `strategy_code_hash` y `origin` quedan en el manifiesto y la auditoría. Sin *timeout* de `signals`: un bucle infinito bloquea un hilo (RFC futuro si hace falta).
- **Precisión `Float32` de las señales**: umbrales cercanos pueden dar señales distintas a una referencia en doble precisión; el *fixture* evita empates y los *golden* detectan la divergencia (A6).
- **Rendimiento de señales**: el cálculo ocurre en el núcleo, fuera del cronómetro del benchmark del motor (`ft_vectorbt` RF-12); un benchmark propio de señales queda pendiente (no bloquea).
- Importar estrategias locales ejecuta su nivel superior en cada arranque del proceso (A23); un error de import se aísla como `invalid`, no detiene el servicio.

## Enmienda G2b — contrato de datos (motor de cortos)

```python
class Signals(NamedTuple):            # strategies/base.py
    entries: pl.Series                # Boolean, largo (sin cambios)
    exits: pl.Series                  # Boolean, largo (sin cambios)
    entries_short: pl.Series          # Boolean, corto (nuevo; siempre poblado)
    exits_short: pl.Series            # Boolean, corto (nuevo; siempre poblado)

def position_to_signals(pos: pl.Series) -> Signals:
    prev = pos.shift(1).fill_null(0)
    direct_reversal = (prev == 1) & (pos == -1) | (prev == -1) & (pos == 1)
    if bool(direct_reversal.any()):
        t = int(direct_reversal.arg_true()[0])
        raise InvalidSignalsError(f"invalid_signals: reversa directa +1<->-1 en t={t} (sin 0 intermedio)")
    return Signals(
        entries=(pos == 1) & (prev != 1),
        exits=(pos != 1) & (prev == 1),
        entries_short=(pos == -1) & (prev != -1),
        exits_short=(pos != -1) & (prev == -1),
    )
```

`ft_vectorbt/engine.py` (`to_framework_input`/`simulate`): agrega `short_entries`/`short_exits`
(`BoolArray`) a `FrameworkInput`; `simulate` los desplaza igual que `entries`/`exits` (RF-5.1) y
los pasa a `vbt.Portfolio.from_signals(..., short_entries=..., short_exits=...)` (sin
`direction="longonly"`: VectorBT soporta ambos lados nativamente vía estos parámetros). La
liquidación final (RF-5.6) fuerza **ambos** `exits[-1] = True` y `short_exits[-1] = True`; solo
el lado con posición abierta genera una operación (VectorBT ignora una señal de salida sin
posición). `_SIDE_SHORT = -1` se promueve a `SIDE_SHORT` en `finances_core.metrics.frames`
(`SIDE_DOMAIN = frozenset({SIDE_LONG, SIDE_SHORT})`); `side` de cada operación ya se deriva de
`records["direction"]` (sin cambios en esa línea, solo dejan de ser alcanzables como "dead code"
porque `direction="longonly"` desaparece).

`CostModel` **no cambia**: `fees_bps`/`slippage_bps` se aplican igual para ambos lados (entrada
corta = "vender para abrir", `sell_price`; salida corta = "comprar para cerrar", `buy_price`;
`costs.py` ya es simétrico, VectorBT aplica su único parámetro `slippage` contra el trader en
ambas direcciones). Sin campo de costo de préstamo (decisión de producto, documentada en
`spec.md`).

**Referencia de verificación independiente** (`tests/contract/reference.py`,
finances-backtester): nueva función Python pura `simulate_short` (sin NumPy/VectorBT) que
calcula P&L de una posición corta — `pnl = (entry_price - exit_price)·size - fees`,
`entry_price` con `sell_price` (deslizamiento en contra), `exit_price` con `buy_price`— para
contrastar el motor real en al menos un caso con cortos antes de mergear (Verification rule,
ítem de riesgo: corrección numérica/dominio).

## Enmienda G3 — máquina de estados largo/corto (contrato de datos)

```python
def position_from_long_short(
    enter_long: pl.Series, exit_long: pl.Series, enter_short: pl.Series, exit_short: pl.Series
) -> pl.Series:
    n = enter_long.len()
    el, xl = enter_long.fill_null(False).to_list(), exit_long.fill_null(False).to_list()
    es, xs = enter_short.fill_null(False).to_list(), exit_short.fill_null(False).to_list()
    out: list[int] = [0] * n
    state, pending = 0, None  # pending: int | None, reversa decidida a aplicar en t+1
    for t in range(n):
        if pending is not None:
            state, pending = pending, None
        elif state == 1:
            if es[t]:
                state, pending = 0, -1
            elif xl[t]:
                state = 0
        elif state == -1:
            if el[t]:
                state, pending = 0, 1
            elif xs[t]:
                state = 0
        else:
            state = 1 if el[t] else (-1 if es[t] else 0)
        out[t] = state
    return pl.Series(out, dtype=pl.Int8)
```

Vive en `strategies/signals.py` (junto a `position_to_signals`), no en `_lib` (que es
`Expr -> Expr`). Cada estrategia materializa sus 4 condiciones con `candles.select(...)` antes
de llamarla. Complejidad `O(n)` en Python puro; a la escala de un backtest (cientos a decenas
de miles de barras) es despreciable frente al resto del pipeline.

**Patrón por estrategia** (ejemplo `sma_cross`, familia direccional):
```python
@staticmethod
def signals(candles: pl.DataFrame, params: SmaCrossParams) -> pl.Series:
    fast, slow = sma(pl.col("close"), params.fast), sma(pl.col("close"), params.slow)
    bullish, bearish = cross_above(fast, slow), cross_below(fast, slow)
    if params.allow_short:
        frame = candles.select(bullish=bullish, bearish=bearish)
        return position_from_long_short(
            enter_long=frame["bullish"], exit_long=frame["bearish"],
            enter_short=frame["bearish"], exit_short=frame["bullish"],
        )
    return candles.select(position_from_events(bullish, bearish)).to_series()
```

**Patrón reversión** (ejemplo `bollinger_reversion`, 4 condiciones distintas):
```python
if params.allow_short:
    frame = candles.select(
        enter_long=close < lower, exit_long=close > middle,
        enter_short=close > upper, exit_short=close < middle,
    )
    return position_from_long_short(
        frame["enter_long"], frame["exit_long"], frame["enter_short"], frame["exit_short"]
    )
return candles.select(position_from_events(close < lower, close > middle)).to_series()
```

**Reflejo `100-x`** (`rsi2_connors`/pierna RSI de `trend_filtered_reversion`): con
`lower`/`upper` existentes, `enter_short = rsi > (100 - lower)`, `exit_short = rsi < (100 - upper)`.

## Enmienda G4 — contrato de datos (stops de riesgo)

```python
class RiskModel(BaseModel):          # finances_core/schemas/backtest.py, junto a CostModel
    model_config = ConfigDict(extra="forbid")
    sl_frac: float | None = Field(default=None, gt=0.0, le=1.0, allow_inf_nan=False)
    tp_frac: float | None = Field(default=None, gt=0.0, le=1.0, allow_inf_nan=False)
    sl_trail: bool = Field(default=False)  # requiere sl_frac; validar con model_validator
# Cota le=1.0: para sl_frac es natural (en un largo no se pierde más del 100%). Para tp_frac
# es un TOPE DELIBERADO de v1 (take-profit ≤ +100% del precio de entrada): VectorBT acepta
# tp_stop > 1.0 y una estrategia de tendencia puede buscar objetivos > 100%, así que esta
# cota se revisará si una estrategia la necesita (decisión del usuario, 2026-11-10).

# BacktestRequest gana:
    risk: RiskModel | None = Field(default=None)

# engine_api.BacktestEngine.run (último parámetro, con default -> aditivo):
def run(self, candles, signals, costs, size_fraction, initial_cash,
        risk: RiskModel | None = None) -> EngineOutput: ...
```

`ft_vectorbt/engine.py::simulate`: si `risk` no es `None`, pasa a
`vbt.Portfolio.from_signals(..., open=inp.open, high=inp.high, low=inp.low,
sl_stop=risk.sl_frac, tp_stop=risk.tp_frac, sl_trail=risk.sl_trail)` (``None`` en
`sl_frac`/`tp_frac` se traduce a `np.nan`/ausente, el default de VectorBT = sin ese stop).
Las mechas `open`/`high`/`low` solo alimentan la detección intrabar de stops; sin `risk` no
se pasan, para preservar byte a byte la salida previa (goldens de las 17 estrategias).

Clasificación de `exit_reason` (`_stop_exit_reasons`/`_classify_stop_reason`): VectorBT no
expone el motivo de salida, así que para cada operación cerrada se **re-deriva** el primer
stop que dispara recorriendo las barras `[entry_idx+1, exit_idx]` contra `high`/`low` de la
vela. El nivel se calcula desde `raw_entry = open[entry_idx]` (precio crudo de ejecución,
**pre-deslizamiento**: la base que usa VectorBT, no el `entry_price` registrado): `sl_level =
basis·(1 - sign·sl_frac)` y `tp_level = raw_entry·(1 + sign·tp_frac)` (`sign = +1` largo,
`-1` corto), con `basis = raw_entry` para el stop fijo y el pico/valle de las barras previas
(excluye la actual y la de entrada) para `sl_trail`. Un stop dispara si la mecha o el `open`
(hueco) cruza el nivel; en empate en la misma barra gana el stop-loss. El primer disparo
fija `EXIT_REASON_STOP_LOSS`/`EXIT_REASON_TAKE_PROFIT`; si ninguno dispara hasta `exit_idx`,
se mantiene `SIGNAL`/`END_OF_DATA`. Nota: la comparación directa `exit_price` vs
`entry_price·(1±frac)` que esbozaba el diseño inicial resultó **insuficiente** (verificado
contra VectorBT real): los huecos llenan al `open`, el trailing usa el pico y el
deslizamiento mueve `entry_price`; por eso se re-deriva el toque sobre la vela. Es detalle
de implementación, sin cambio del contrato ni del comportamiento observable.

El stop fija `exit_price` en el nivel teórico, salvo hueco (la barra abre pasada el nivel)
en cuyo caso llena al `open`; los stops **no** sufren deslizamiento, pero sí comisión de
salida. Las salidas por señal y la liquidación de fin de datos sí sufren deslizamiento.

**Invariante a vigilar** (acoplamiento con VectorBT): el `exit_reason` se re-deriva de las
mechas de forma **independiente** de la decisión interna de VectorBT (el precio y el
índice de salida sí vienen de VectorBT). La clasificación asume el modelo verificado contra
VectorBT 1.2.0 — en particular que el pico del trailing **excluye** la barra en curso y la de
entrada. Si se actualiza VectorBT o cambia su temporización del trailing, hay que re-verificar
esa convención contra la referencia de `tests/contract/` (un desajuste reetiquetaría un
`sl_trail` como señal); el test de contraste motor↔referencia es la red que lo caza.

**Referencia de verificación independiente** (`tests/contract/reference.py`,
finances-backtester): nueva función Python pura `simulate_with_stops` (sin
VectorBT/NumPy) que, dada una secuencia de precios y un `RiskModel`, determina a mano
si el stop o el take-profit se toca primero (comparación barra a barra contra
`high`/`low` de la vela, no solo `close`) y calcula el PnL resultante, para contrastar el
motor real en casos largos y cortos con stop, con take-profit, y con `sl_trail`.

## Enmienda G5 — contrato de datos (evaluación honesta: expectancy-R + bootstrap)

```python
class EvaluationReport(BaseModel):          # finances_core/metrics/evaluation.py
    model_config = ConfigDict(extra="forbid")
    n_trades: int
    sl_frac: float | None                    # de manifest.request.risk.sl_frac
    expectancy_cash: float | None            # referencia (= LedgerMetrics.expectancy)
    expectancy_r: float | None               # media de R; None si sl_frac None o sin trades
    r_ci_low: float | None                   # IC bootstrap (percentil inferior)
    r_ci_high: float | None                  # IC bootstrap (percentil superior)
    ci_level: float                          # p. ej. 0.95
    n_resamples: int                         # p. ej. 10000
    seed: int                                # semilla usada (determinismo)
    min_trades: int                          # mínimo de operaciones para declarar edge
    sufficient_sample: bool                  # n_trades >= min_trades
    has_edge: bool                           # sufficient_sample and r_ci_low > 0

DEFAULT_CI_LEVEL = 0.95
DEFAULT_N_RESAMPLES = 10_000
DEFAULT_SEED = 1234
DEFAULT_MIN_TRADES = 30

def compute_evaluation(
    trades: pl.DataFrame, *, sl_frac: float | None,
    ci_level: float = DEFAULT_CI_LEVEL, n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = DEFAULT_SEED, min_trades: int = DEFAULT_MIN_TRADES,
) -> EvaluationReport: ...
#   R = pnl / (entry_price * size * sl_frac)         (por operación; Float32 -> escalar)
#   expectancy_r = mean(R)
#   bootstrap IID: para k en 1..n_resamples, remuestrear n operaciones con reemplazo (RNG
#   sembrado), tomar la media de R; r_ci_low/high = percentiles (1-ci)/2 y (1+ci)/2 de esas
#   n_resamples medias.
#   sufficient_sample = n_trades >= min_trades; has_edge = sufficient_sample and r_ci_low > 0.
#   (Con muy pocas operaciones el IC es degenerado -> no se afirma edge; anti-p-hacking.)
```

Superficie (dualidad CLI/API, plan §1.4 y §3):
- **CLI**: `ft_vectorbt eval <run_id> [--ci-level 0.95] [--n-resamples 10000] [--seed 1234]
  [--min-trades 30]` → imprime `EvaluationReport`; usa el flag **global** `--format json|table`
  (como el resto de los comandos) para elegir el formato.
- **API**: `GET /backtest/vectorbt/runs/{run_id}/eval?ci_level=&n_resamples=&seed=&min_trades=`
  → `EvaluationReport`.
- Ambos resuelven la corrida con `results_store.find_run(run_id)`, leen `manifest.json` (de ahí
  `sl_frac = request.risk.sl_frac`) y `trades.parquet`, y llaman `compute_evaluation`. Read-only;
  nunca recomputan la simulación. Corrida inexistente → error "run not found" (contrato idéntico
  en CLI y API).

**Fase posterior (fuera de G5)**: consistencia por símbolo — correr la estrategia en N símbolos
y agregar la fracción con `expectancy_r > 0` (o `has_edge`); requiere una capa de orquestación
multi-símbolo que hoy no existe. Se especifica en su propia enmienda.

## Enmienda L2 — contrato de datos (expansión de `_lib`)

Todo en `finances_core/backtest/strategies/_lib/`. Aditivo: no cambia `signals(candles, params)`.

```python
# _lib/__init__.py (vectorizables, pl.Expr -> Float32, prefijo-estable, null en calentamiento)
def plus_di(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr: ...
def minus_di(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr: ...
def adx(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr: ...
def stoch_k(high: pl.Expr, low: pl.Expr, close: pl.Expr, k: int) -> pl.Expr: ...
def stoch_d(high: pl.Expr, low: pl.Expr, close: pl.Expr, k: int, d: int) -> pl.Expr: ...
def cci(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr: ...
def williams_r(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr: ...
def mfi(high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr, n: int) -> pl.Expr: ...
def bbands_pctb(close: pl.Expr, n: int, k: float) -> pl.Expr: ...        # %b = (c-low)/(up-low)
def bbands_bandwidth(close: pl.Expr, n: int, k: float) -> pl.Expr: ...   # (up-low)/mid
def ichimoku(high: pl.Expr, low: pl.Expr, tenkan: int, kijun: int, senkou_b: int,
             ) -> dict[str, pl.Expr]: ...  # {tenkan, kijun, senkou_a, senkou_b}, causal

# _lib/stateful.py (recursión condicional; scan causal puro, pl.Series -> pl.Series Float32)
def supertrend(high: pl.Series, low: pl.Series, close: pl.Series, period: int,
               multiplier: float) -> tuple[pl.Series, pl.Series]: ...   # (supertrend, trend{-1,+1})
def sar(high: pl.Series, low: pl.Series, acceleration: float, maximum: float) -> pl.Series: ...

# _lib/patterns.py (booleanos pl.Expr; True = patrón en t; validados vs CDL* de TA-Lib)
def doji(...): ...; def hammer(...): ...; def shooting_star(...): ...
def bullish_engulfing(...): ...; def bearish_engulfing(...): ...; def harami(...): ...
def marubozu(...): ...; def piercing(...): ...; def dark_cloud_cover(...): ...
def morning_star(...): ...; def evening_star(...): ...
```

Fórmulas clave (vs TA-Lib):
- `+DM = max(high-high[t-1], 0)` si `> (low[t-1]-low)` si no 0; `-DM` simétrico. `TR` como `atr`.
  `+DI = 100·Wilder(+DM,n)/Wilder(TR,n)`; `-DI` idem. `DX = 100·|+DI - -DI|/(+DI + -DI)`;
  `ADX = Wilder(DX, n)` (doble suavizado Wilder). Float64 interno (cociente/cancelación).
- `stoch_k = 100·(close - min(low,k)) / (max(high,k) - min(low,k))`; `stoch_d = SMA(stoch_k, d)`.
- `cci = (tp - SMA(tp,n)) / (0.015 · desviación media de tp)`, `tp=(h+l+c)/3`; Float64 interno.
- `williams_r = -100·(max(high,n) - close)/(max(high,n) - min(low,n))`.
- `mfi`: *money flow* tipico ±, ratio sobre ventana n; Float64 interno.
- `bbands`: `mid=SMA(close,n)`, `sd=rolling_std_stable(close,n)`, `up/low = mid ± k·sd`.
- `ichimoku`: `tenkan=(rollmax(high,t)+rollmin(low,t))/2`, `kijun` idem kijun,
  `senkou_a=((tenkan+kijun)/2)` desplazado causalmente, `senkou_b=(rollmax(high,sb)+rollmin(low,sb))/2`
  desplazado. El *forward shift* se modela como `.shift(kijun)` (el valor de hace `kijun` barras),
  nunca mirando el futuro (RF-20).

Validación (dual, por indicador):
- **finances-core** `tests/unit/test_strategies_lib*.py`: *golden* hand-computed (fórmula en el
  comentario), `pl.Float32`, `null` en calentamiento, test de causalidad para los con estado.
- **cross-validación viva vs TA-Lib**: test en `finances-analytics`/`finances-api` (tienen `talib`)
  que importa `finances_core...._lib`, corre contra una serie OHLCV determinista y compara con
  `talib` dentro de tolerancia (RFC-0004). Caza *drift* de versión de TA-Lib.

**Tandas** (cada una test-first, aditiva): (L2a) direccional ADX/+DI/−DI; (L2b) osciladores
stoch/cci/willr/mfi; (L2c) bandas + ichimoku; (L2d) con estado supertrend/sar; (L2e) patrones.

## Enmienda L3 — contrato de datos (VWAP anclado a sesión)

Todo en `finances_core/backtest/strategies/_lib/__init__.py`. Aditivo: no cambia
`signals(candles, params)`.

```python
# pl.Expr -> Float32, anclado a sesión (cum_sum().over(session)), null si Σvolume==0 en la sesión
def session_vwap(high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr,
                 session: pl.Expr) -> pl.Expr: ...        # Σ(tp·v)/Σ(v) acumulado por sesión
def session_vwap_sigma(high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr,
                       session: pl.Expr) -> pl.Expr: ...  # sqrt(E_v[tp²]-vwap²), ≥0, Float64 interno
```

Fórmulas:
- `tp=(high+low+close)/3`; `session_vwap = Σ(tp·v)/Σ(v)` acumulado por sesión; `null` si `Σv==0`.
  Cociente de sumas positivas → `Float32` puro (sin cancelación).
- `session_vwap_sigma = sqrt(E_v[tp²] − vwap²)` con `E_v[tp²]=Σ(tp²·v)/Σ(v)`; **Float64 interno**
  (cancelación, excepción RFC-0007 como `rolling_std_stable`), `clip(≥0)`, salida `Float32`.
- `session` lo provee el consumidor (id causal por sesión, p. ej. fecha ET derivada de
  `open_time_ms`); la primitiva es genérica (sin TZ/horario). Área de valor en la estrategia:
  `VAH/VAL = session_vwap ± k·session_vwap_sigma`.

Validación: golden hand-computed en `tests/unit/test_strategies_lib.py` (sesión + reset en el borde,
`Σv==0→null`, prefijo-estable/causal). `session_vwap_sigma` en `DOUBLE_EXCEPTIONS`. Sin equivalente
TA-Lib → sin cross-validación viva.
