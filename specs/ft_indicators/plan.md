# Plan — `ft_indicators` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquemas (`packages/finances-core/src/finances_core/schemas/indicators.py`)
Viven en `finances_core` porque el futuro backtester necesita leer la nomenclatura y el manifiesto sin importar
`ft_indicators` (Art. 4).
```python
IndicatorName = Literal["sma", "ema", "wma", "rsi", "macd", "bb", "atr",
                        "stoch", "roc", "obv", "adx", "vwap"]
PriceSource = Literal["open", "high", "low", "close", "adj_close"]
IndicatorPhase = Literal["PENDING", "PLANNING", "COMPUTING", "COMPLETED", "FAILED"]

class IndicatorSpec(BaseModel):
    name: IndicatorName
    params: dict[str, int | float | str] = {}     # validado contra el catálogo (RF-2) en el servicio

class IndicatorComputeRequest(BaseModel):
    datasets: list[DatasetIdentifier] = Field(min_length=1, max_length=100)
    indicators: list[IndicatorSpec] = Field(min_length=1, max_length=50)

class IndicatorPlanItem(BaseModel):
    dataset: DatasetIdentifier
    indicator_id: str
    spec: IndicatorSpec                            # con defaults resueltos
    columns: list[str]

class IndicatorPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: list[IndicatorPlanItem]

class Stability(BaseModel):                         # RF-16
    stable_from_ms: int | None                     # indicador = máximo de sus columnas
    by_column: dict[str, int | None]
    rule: str                                      # p. ej. "ewm alpha=2/21 eps=1e-6 seed_idx=19 k=139"
    reason: Literal["converged", "first_non_null", "non_decaying_seed", "not_converged"]

class IndicatorProgress(BaseModel):
    task_id: str
    status: IndicatorPhase
    datasets_total: int
    datasets_done: int
    items_total: int
    items_done: int
    rows_computed: int
    partitions_written: int
    stability: dict[str, Stability]                # clave "ex/mt/sym/tf:indicator_id", se llena al terminar cada item
    warnings: list[str]
    errors: list[str]

class IndicatorItemResult(BaseModel):
    dataset: DatasetIdentifier
    indicator_id: str
    columns: list[str]
    rows: int
    first_ms: int | None
    last_ms: int | None
    stability: Stability
    partitions_written: int
    partitions_removed: int
    warnings: list[str]                            # insufficient_history, not_converged, null_inputs=N

class IndicatorComputeResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    items: list[IndicatorItemResult]
    output_dirs: list[str]                         # un directorio timeframe=... por dataset
    warnings: list[str]
    errors: list[str]

class DispatchResponse(BaseModel):                 # mismo contrato que los descargadores
    task_id: str
    status: Literal["ACCEPTED"]
    poll_url: str
    plan_url: str
    result_url: str

class IndicatorCatalogEntry(BaseModel):            # GET /catalog
    name: IndicatorName
    params: dict[str, dict[str, object]]           # {param: {type, default, min, max}}
    inputs: list[str]
    outputs: list[str]                             # sufijos de salida ([] si única)
    warmup: str                                    # fórmula legible, p. ej. "period-1"
    stable_from: str                               # fórmula legible RF-16, p. ej. "period-1 + ceil(ln(1e-6)/ln(1-2/(period+1)))"
    f32_tolerance: dict[str, dict[str, float]]     # {columna: {rtol, atol, atol_scale}} (§3.9)
    engine_version: int
    convention: str

class ManifestEntry(BaseModel):
    spec: IndicatorSpec
    indicator_id: str
    columns: list[str]
    engine_version: int
    rows: int
    first_ms: int | None
    last_ms: int | None                            # último open_time_ms calculado (detección de desactualización)
    stability: Stability                           # RF-16
    computed_at_ms: int

class IndicatorManifest(BaseModel):                # GET /datasets/{...}
    dataset: DatasetIdentifier
    lock_task_id: str | None = None
    indicators: dict[str, ManifestEntry]

class IndicatorExportRequest(BaseModel):
    start_ms: int | None = None
    end_ms: int | None = None                      # exclusivo
    indicators: list[str] | None = None            # indicator_id; None = todos los calculados
    columns: list[str] | None = None               # columnas concretas (alternativa a indicators)
    include_ohlcv: bool = False
    stability: Literal["all", "flag", "drop"] = "all"   # RF-9/RF-16
    format: ExportFormat = "parquet"               # finances_core.export
    output_dir: str | None = None                  # None/"-" = stream
```
Funciones puras de nomenclatura (RF-4) en el mismo módulo: `format_param(value) -> str`,
`indicator_id(spec_resuelta) -> str`, `output_columns(spec_resuelta, outputs) -> list[str]`.
`DatasetIdentifier` se reutiliza de `finances_core.schemas.data_viewer` (con `market_type` ampliado por la rama
`ft_market_data_yfinance`, ver §7) y gana `DatasetIdentifier.parse("exchange/market_type/symbol/timeframe")` y
`.path_segment()`.

### 1.2 Esquemas Polars
```python
INDICATOR_FILE_SCHEMA = {"open_time_ms": pl.Int64}          # + columnas de salida pl.Float32
EXPORT_ID_COLUMNS = ["exchange", "market_type", "symbol", "timeframe", "open_time_ms"]
OHLCV_EXPORT_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]   # adj_close si existe
```

### 1.3 Configuración (`finances_core/config.py`)
```python
INDICATORS_DIR: pathlib.Path = pathlib.Path("./data/indicators")   # resuelto desde PROJECT_ROOT
INDICATORS_MAX_CONCURRENT: int = 2                                 # ge=1, le=16
```
Validación (RF-7): `INDICATORS_DIR` no puede estar dentro de `PARQUET_DIR` → `ConfigError`.

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-analytics/src/finances_analytics/ft_indicators/cli.py`
```
ft_indicators [-v] [-q] [--log-format rich|json] COMMAND
  catalog                                   # JSON == GET /catalog
  compute   -d/--dataset EX/MT/SYM/TF... -i/--indicator NAME[:k=v,...]... [--dry-run]
            | --request-file FILE.json [--dry-run]
  plan      TASK_ID                         # JSON == GET /plan/{task_id}
  status    TASK_ID                         # JSON == GET /status/{task_id}
  result    TASK_ID                         # JSON == GET /result/{task_id}
  show      EX/MT/SYM/TF                    # JSON == GET /datasets/{...}
  export    EX/MT/SYM/TF [-i/--indicator ID_O_SPEC...] [--column COL...]
            [--start T] [--end T] [--include-ohlcv] [--stability all|flag|drop]
            [--format parquet|csv|jsonl] [-o/--output-dir DIR|-]
Exit: 0 OK · 1 no encontrado/bloqueado/genérico · 2 validación · 3 tarea FAILED o export vacío · 4 IO
```
- Gramática `--indicator` (`spec_parser.py`): `name(':' key '=' value (',' key '=' value)*)?`; valores enteros,
  decimales o identificadores (`source`). Produce `IndicatorSpec`; misma validación que la API.
- `--start/--end` aceptan lo mismo que `finances_core.timeutils.parse_time_input`.
- `compute` crea la tarea, ejecuta el servicio en proceso (`asyncio.run`) con barra Rich por dataset e imprime
  la tabla final; `--dry-run` imprime el `IndicatorPlan` sin crear tarea ni escribir.
- `export` sin `-o` o con `-o -` → stdout binario; el resumen va a stderr.

### 2.2 API (FastAPI) — `.../ft_indicators/api.py`, `router = APIRouter(prefix="/indicators")`
| Método | Ruta | Respuesta |
|---|---|---|
| GET | `/catalog` | `list[IndicatorCatalogEntry]` |
| POST | `/compute` (cuerpo `IndicatorComputeRequest`) | `202` `DispatchResponse` · 400 · 404 dataset · 409 bloqueado |
| GET | `/status/{task_id}` | `IndicatorProgress` · 404 |
| GET | `/plan/{task_id}` | `IndicatorPlan` · 404 |
| GET | `/result/{task_id}` | `IndicatorComputeResult` · 404 · 409 si no terminó |
| GET | `/datasets/{exchange}/{market_type}/{symbol}/{timeframe}` | `IndicatorManifest` · 404 |
| POST | `/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export` (cuerpo `IndicatorExportRequest`) | `StreamingResponse` (`Content-Type` por formato, `Content-Disposition`, `X-FT-Indicators-Stale` opcional) · 400 · 404 |

- `task_id` validado con `^[0-9a-f-]{36}$`. Montado por `finances_api.create_app()`.
- `POST /compute`: valida, comprueba existencia de datasets (solo `stat` de directorios) y bloqueos, crea la tarea
  `PENDING` y encola `run_download_sync(factory, task_id)` en `BackgroundTasks`; la planificación ocurre dentro
  de la tarea (fase `PLANNING`) para responder en ≤ 200 ms.
- Exportación: se resuelve en `run_in_threadpool` a un directorio temporal y se sirve con `StreamingResponse`
  + `BackgroundTask` de limpieza (mismo patrón que `ft_data_viewer`).

## 3. Algoritmos Clave

### 3.1 Catálogo — `catalog.py`
```python
@dataclass(frozen=True)
class ParamDef:
    name: str; type: type[int] | type[float] | type[str]; default: object
    min: float | None = None; max: float | None = None; choices: tuple[str, ...] | None = None

@dataclass(frozen=True)
class IndicatorDef:
    name: IndicatorName
    params: tuple[ParamDef, ...]          # orden = orden en indicator_id
    inputs: Callable[[ResolvedParams], tuple[str, ...]]
    outputs: tuple[str, ...]              # () = salida única
    warmup: Callable[[ResolvedParams], int]
    stages: Callable[[ResolvedParams], Mapping[str, StageGraph]]   # por columna: etapas (alpha, seed_idx, entrada) para RF-16
    f32_tolerance: Mapping[str, Tolerance]                         # §3.9
    kernel: Kernel                        # §3.3
    engine_version: int                   # se incrementa ante cualquier cambio de fórmula (queda en el manifiesto)
    convention: str

CATALOG: Mapping[IndicatorName, IndicatorDef]
def resolve(spec: IndicatorSpec, available_columns: set[str]) -> ResolvedIndicator   # defaults + RF-2/RF-6
```

### 3.2 Fuente — `source.py`
- `resolve_dataset_dir(settings, ds)` vía `finances_core.marketdata.inventory.partition_base_dir`.
- `load_source(ds, columns) -> pl.DataFrame`: `pl.scan_parquet` de todos los `year=*/month=*/data.parquet`, solo
  `open_time_ms` + entradas (unión de las de todos los indicadores pedidos), ordenado, `collect()` una vez.
- `validate_source(df)` (Art. 13): columnas y dtypes canónicos, `open_time_ms` estrictamente creciente y único;
  `NaN/inf` → null (`fill_nan(None)`, `is_finite`); devuelve conteo de nulos para `warnings`.

### 3.3 Kernels — `kernels/` (expresiones Polars puras, cálculo `Float64`)
Firma única: `Kernel = Callable[[pl.DataFrame, ResolvedParams], pl.DataFrame]` → salidas `Float64` alineadas 1:1.
Las entradas `Float32` se convierten a `Float64` al entrar al kernel; el motor (§3.4) convierte a `Float32` antes de
persistir. `Float64` no sale de `kernels/` + `engine.py` salvo hacia el paquete del oráculo (§3.10).

| Archivo | Indicadores | Técnica |
|---|---|---|
| `windowed.py` | `sma`, `wma`, `bb`, `roc`, `vwap`, `stoch` | `rolling_*`/`shift` exigiendo ventana completa (ventana con nulo → nulo); `bb` σ con `ddof=0`; `stoch` HH/LL con `rolling_max/min` |
| `recursive.py` | `ema`, `rsi`, `macd`, `atr`, `adx` | Semilla SMA insertada en la fila de semilla y `ewm_mean(alpha=…, adjust=False, ignore_nulls=True)` sobre la subsecuencia no nula; Wilder = α = 1/period; sumas de Wilder de ADX = media de Wilder × period |
| `cumulative.py` | `obv` | `sign(diff(close)) * volume` → `cum_sum` (primera fila = volume_0) |

Convenciones de división por cero (RF-2: RSI sin movimiento = 0 y Estocástico con HH = LL = 0, como TA-Lib) con
`pl.when(...).then(...)`.

### 3.4 Motor — `engine.py` (recálculo completo)
- Un único `df.select(*expresiones de todos los indicadores pedidos)` sobre la serie completa: Polars paraleliza
  columnas; determinista (misma entrada ⇒ misma salida), lo que da idempotencia por construcción (RF-8).
- Garantías verificadas por tests: alineación 1:1 con la fuente, calentamiento nulo, sin anticipación (prefijo
  estable), cálculo `Float64` y salida `Float32` (`cast` único en la frontera).
- Tras calcular, `convergence.stability(def, params, non_null_index_map)` (§3.9) produce `Stability` por columna.
- Historia insuficiente ⇒ columna nula + `insufficient_history`.
- Datasets en paralelo: `asyncio.to_thread` + semáforo `FT_INDICATORS_MAX_CONCURRENT`.
- Memoria: 1M filas × (6 entradas + 19 salidas) × 8 B (`Float64` interno) ≈ 200 MB por dataset, más intermedios;
  aceptable con 2 datasets en paralelo.

### 3.5 Planificación — `planner.py`
`plan_items(request, settings) -> IndicatorPlan` (puro, sin cálculo): resuelve specs, `indicator_id` y columnas;
verifica columnas disponibles (RF-6). Persiste con
`finances_core.marketdata.plans.save_plan(task_id, plan, prefix="indicators_plan")`.

### 3.6 Almacenamiento — `store.py`
- Reescritura por indicador: `df` (`open_time_ms` + columnas del indicador + `year`/`month`) se escribe con
  `finances_core.marketdata.hive_writer.write_parquet_partitioned(df, settings.INDICATORS_DIR, exchange=…,
  schema=…, file_name=f"{indicator_id}.parquet", mode="replace")`: cada mes se reemplaza de forma atómica con
  todas sus filas (sin pérdida respecto de la fuente); después se borran los `{indicator_id}.parquet` de meses sin
  filas fuente.
- **Por qué un archivo por indicador** (y no una tabla ancha por dataset): con recálculo completo de solo los
  indicadores pedidos, una tabla ancha obligaría a leer y reescribir también las columnas no pedidas (o dejarlas
  sin filas nuevas, desalineadas); con un archivo por indicador cada uno es autocontenido, añadir uno no toca los
  demás y el reemplazo atómico es por archivo. La tabla ancha la produce `export`.
- Manifiesto: `read_manifest`/`write_manifest` (JSON validado con `IndicatorManifest`, escritura atómica
  `tmp` + `os.replace`), escrito **después** de las particiones; solo se actualizan las entradas pedidas.
- Bloqueo: `lock_task_id` en el manifiesto + archivo `_lock` creado con `O_EXCL`; obsoleto si la tarea dueña es
  terminal o no existe en `tasks.db` (RF-8).

### 3.7 Exportación — `exporter.py`
`build_export_lazyframe(ds, req) -> (pl.LazyFrame, stale: list[str])`: base = `open_time_ms` de la fuente en
`[start, end)` (si `include_ohlcv`) o unión de los `open_time_ms` de los indicadores; `join(how="left",
on="open_time_ms")` de cada indicador (`scan_parquet` por `indicator_id` con poda Hive por año/mes); columnas
identidad constantes. `stale` = indicadores cuyo `last_ms` del manifiesto es menor que el máximo `open_time_ms`
de la fuente. `stability`: `flag` añade `pl.col("open_time_ms") >= stable_from_ms` como `{indicator_id}_is_stable`;
`drop` filtra `open_time_ms >= max(stable_from_ms)` (nulo ⇒ error de validación). Streaming con
`finances_core.export.stream_export(lf, fmt, output)`.

### 3.8 Orquestador — `service.py`
Reusa `finances_core.marketdata.tasks_lifecycle` (`create_download_task`, `TaskRecorder` con
`phase_order=("PENDING","PLANNING","COMPUTING")`, `run_download_sync`, `ensure_finished`):
```
create_task (PENDING) → PLANNING (planner + save_plan) → COMPUTING (por dataset: lock → load_source →
validate → engine → store → manifest → unlock; contadores en progress) → COMPLETED | FAILED (solo IO/inesperado)
```
Funciones públicas: `dispatch_compute(request) -> DispatchResponse`, `run_compute(task_id, request, *, on_progress)`,
`get_progress/get_plan/get_result(task_id)`, `get_manifest(ds)`, `export(ds, req, *, stream=None)`, `catalog()`.

### 3.9 Convergencia y política de precisión — `convergence.py`, `precision.py`
**Convergencia (RF-16)**, función pura sin datos:
```python
EPSILON = 1e-6                                     # parte de engine_version
def k_star(alpha: float, eps: float = EPSILON) -> int:          # ceil(ln eps / ln(1-alpha))
def stable_index(graph: StageGraph) -> int | None:              # max(seed_idx, stable(entrada)) + k_star; None si no decae
def stability(defn, params, non_null_positions) -> Stability    # índice sobre filas no nulas -> open_time_ms
```
| Indicador (default) | Etapas | Índice estable |
|---|---|---|
| `ema_20` | EMA α=2/21, semilla 19, k*=139 | 158 |
| `rsi_14` | Wilder α=1/14 sobre G y L, semilla 14, k*=187 | 201 |
| `atr_14` | Wilder α=1/14, semilla 14 | 201 |
| `adx_14` | ±DI: Wilder semilla 14 → 201; ADX: max(27, 201) + 187 | ±DI 201; ADX 388 |
| `macd_12_26_9` | fast α=2/13 semilla 11, k*=83 → 94; slow α=2/27 semilla 25, k*=180 → 205; signal α=0,2, k*=62 | line 205; signal/hist 267 |
| `obv` | acumulado, semilla volume₀ no decae | nulo (`non_decaying_seed`) |
| no recursivos | — | primera fila no nula |

**Precisión** (decisión): calcular en `Float64`, **persistir en `Float32`** (Art. 5).
- Calcular en `Float64` hace al oráculo independiente del redondeo `f32` (lo que se quiere medir en GPU) y evita
  la deriva de sumas móviles y acumulados largos en `f32`. Excepción del Art. 5.3: aislada en `kernels/` + `engine.py`
  (frontera única de `cast`), justificada aquí y en ADR-I11; se registra como RFC según la constitución.
- **No** se persiste en `Float64`: los consumidores (backtester, kernels GPU, notebooks) trabajan en `f32`, el Art. 5
  lo exige en datos persistidos, duplicaría el almacenamiento, y el rol de oráculo lo cubre el paquete de *fixtures*
  `Float64` (§3.10), que es un artefacto de test.

**Tolerancias esperadas para implementaciones `f32`** (provisionales; las confirma la futura spec GPU). Se comparan
solo filas `≥ stable_from_ms` con `|x_f32 − x_oráculo| ≤ atol + rtol·|x_oráculo|`; `escala` = media de `|close|` de la
serie (o de `volume` acumulado en `obv`). Fuera de la tolerancia: nulos y convenciones de división por cero **exactos**.

| Columna(s) | rtol | atol | Bit a bit esperable | Motivo |
|---|---|---|---|---|
| `sma`, `wma`, `vwap`, `bb_*_middle` | 1e-5 | 0 | No (orden de suma) | Sumas de ventana en `f32` |
| `ema` | 1e-5 | 0 | Solo en la misma secuencia de operaciones | Recursión; error de semilla < ε desde `stable_from` |
| `atr` | 1e-5 | 0 | No | Recursión de Wilder |
| `bb_*_upper/lower` | 1e-5 | 1e-5·escala | No | `sqrt` de varianza; cancelación si se usa E[x²]−E[x]² |
| `macd_*_line/signal/hist` | 0 | 1e-5·escala | No | Diferencia de EMAs cercanas (cancelación) |
| `rsi` | 0 | 1e-3 (puntos) | No | División G/(G+L) |
| `stoch_*_k/d` | 0 | 1e-3 (puntos) | No | División por HH−LL pequeño |
| `adx`, `adx_*_plus_di/minus_di` | 0 | 1e-2 (puntos) | No | Divisiones encadenadas y doble suavizado |
| `roc` | 0 | 1e-4 (puntos %) | No | Cociente |
| `obv` | 0 | 1e-6·Σ\|volume\| | No | Acumulado largo en `f32` |

La tabla vive en `precision.py` (fuente única) y se publica en `GET /catalog` y en el paquete del oráculo.

### 3.10 Paquete de *fixtures* del oráculo — `oracle.py` + `scripts/export_oracle_fixtures.py`
Herramienta de desarrollo (no es funcionalidad de usuario; no requiere CLI/API, Art. 1 no aplica):
`export_oracle_pack(out_dir: Path, version: str) -> Path`, ejecutada por el script y versionada en
`packages/finances-analytics/tests/ft_indicators/fixtures/oracle/v1/`:
```
oracle/v1/
├── manifest.json                    # schema_version, engine_versions, EPSILON, tolerancias §3.9, convenciones,
│                                    # commit generador, sha256 de cada archivo
├── inputs/{case}.parquet            # open_time_ms Int64 + OHLCV Float32 (esquema canónico) + copia .csv
└── expected/{case}/{indicator_id}.parquet   # open_time_ms + columnas Float64 + stable_from_index por columna (en manifest)
```
Casos: paseo aleatorio 10.000 velas (semilla fija), serie plana, serie con nulos/NaN intercalados, serie más corta
que el calentamiento, precios extremos (1e-6 y 1e6), ADX/estocástico con HH = LL. Parquet + CSV (valores `Float64`
con `repr` de 17 dígitos) para que un test WGSL/CUDA/C++ los lea sin Python. Un test regenera el paquete y exige
igualdad con lo versionado (detecta cambios de fórmula no versionados).

## 4. Estructura de Archivos Objetivo
```
packages/finances-core/src/finances_core/
├── export.py                       # NUEVO: stream_export, ExportFormat, MEDIA_TYPES, validate_output_dir (de ft_data_viewer)
├── marketdata/hive_writer.py       # + file_name="data.parquet", mode="merge"|"replace" (retrocompatible)
├── marketdata/plans.py             # + prefix="download_plan" en plan_file_path/save_plan/load_plan
├── schemas/data_viewer.py          # DatasetIdentifier.parse / path_segment
└── schemas/indicators.py           # NUEVO: contratos §1.1 + nomenclatura RF-4
packages/finances-analytics/
├── pyproject.toml
├── scripts/generate_talib_fixtures.py   # SOLO fuera de CI (requiere TA-Lib local); no es dependencia del paquete
├── scripts/export_oracle_fixtures.py    # genera tests/ft_indicators/fixtures/oracle/v1 (§3.10)
├── src/finances_analytics/
│   ├── __init__.py  py.typed
│   └── ft_indicators/
│       ├── __init__.py  catalog.py  spec_parser.py  source.py  engine.py  planner.py
│       ├── store.py  exporter.py  service.py  errors.py  api.py  cli.py
│       ├── convergence.py  precision.py  oracle.py
│       └── kernels/  __init__.py  windowed.py  recursive.py  cumulative.py
└── tests/ft_indicators/
    ├── conftest.py                 # FT_PROJECT_ROOT temporal + fábrica de datasets Hive sintéticos
    ├── fixtures/reference/*.csv    # StockCharts / Wilder / TA-Lib (offline) con cabecera de procedencia
    ├── fixtures/oracle/v1/         # paquete del oráculo (§3.10)
    ├── test_*.py
    └── benchmarks/test_bench_indicators.py   # marcador `benchmark` (RF-14)
```

## 5. Dependencias (pyproject.toml — finances-analytics)
- `finances-core`, `polars>=1.6`, `pydantic>=2.8`, `pydantic-settings>=2.3`, `typer>=0.12`, `fastapi>=0.111`,
  `structlog>=24.1`, `rich>=13.7`, `uuid6>=1.11`, `pyarrow>=16.0`. **Nunca** `finances-downloader`, `pandas` ni
  `TA-Lib` (ni en runtime ni en `dev`).
- `dev`: `pytest>=8.2`, `pytest-asyncio>=0.23`, `httpx>=0.27`, `ruff>=0.5`, `mypy>=1.10`, `hypothesis>=6.100`
  (tests de propiedad de RF-3).
- `scripts/generate_talib_fixtures.py` importa `talib` y queda excluido de mypy/pytest/ruff-imports de CI; se
  ejecuta a mano en un entorno aparte.
- Script: `ft_indicators = "finances_analytics.ft_indicators.cli:app"`.
- Configuración de ruff/mypy/pytest copiada de `finances-downloader` (+ marcador `benchmark` excluido por defecto
  con `addopts = "-m 'not benchmark'"`).

## 6. Decisiones Arquitectónicas Clave (ADR)
| ID | Decisión | Motivo |
|---|---|---|
| ADR-I01 | Nuevo paquete `finances-analytics`, dependiente solo de `finances-core` | Separa cómputo analítico de la ingesta; el backtester futuro puede vivir allí sin arrastrar `yfinance`/`httpx` |
| ADR-I02 | Implementaciones propias en Polars, sin TA-Lib/pandas en runtime ni CI | Art. 8; sin binarios nativos difíciles en Windows; control total de convenciones |
| ADR-I03 | Convenciones Wilder/TA-Lib (RSI, ATR, ADX, EMA con semilla SMA, casos planos = 0); MACD con EMAs de semilla propia (StockCharts) | Referencias publicadas y verificables; desviaciones documentadas en el catálogo |
| ADR-I04 | Recálculo completo por dataset en cada ejecución | Simple, determinista e idempotente por construcción; 1M filas cuestan segundos (RF-14). Incremental = mejora futura (No-Go) |
| ADR-I05 | Un archivo por indicador y mes (`{indicator_id}.parquet`) con las mismas claves Hive que la fuente | Reescribir solo lo pedido sin tocar otros indicadores; reemplazo atómico por archivo |
| ADR-I06 | `close` crudo por defecto; `adj_close` opt-in | Coherencia con H/L crudos y con lo que se opera |
| ADR-I07 | VWAP móvil sí, VWAP de sesión no | Bien definido 24/7 y en cualquier timeframe; la sesión requiere calendario inexistente en el esquema |
| ADR-I08 | Petición canónica única (`IndicatorComputeRequest`) con gramática CLI y `--request-file` | CLI ≡ API verificable por igualdad de modelos (Art. 1) |
| ADR-I09 | `export` y nomenclatura en `finances_core` | Art. 4: `ft_indicators` no importa `ft_data_viewer`; el backtester no importa `ft_indicators` |
| ADR-I10 | *Fixtures* TA-Lib generados offline y versionados | Contraste independiente sin dependencia nativa en CI |
| ADR-I11 | Cálculo interno `Float64`, persistencia `Float32` | Rol de oráculo (independiente del redondeo `f32` que se quiere medir) sin violar el Art. 5 en datos persistidos; excepción del Art. 5.3 aislada en `kernels/`+`engine.py` |
| ADR-I12 | `ft_indicators` es el oráculo de referencia de los futuros kernels GPU `f32` | WGSL no tiene `f64`; RSI/Bollinger/MACD nunca serán bit a bit en `f32` (experimentos WGSL del Tech Lead): se valida con tolerancias por columna y paquete de *fixtures* estable |
| ADR-I13 | `stable_from_ms` analítico (ε = 1e-6) además de los nulos de calentamiento | La semilla de las EMA contamina ~160 velas (EMA 20: 158); el consumidor decide descartarlas sin perder la alineación 1:1 |

## 7. Integración con `finances-core` y la suite
- **Dependencia de orden**: la rama de `ft_indicators` **arranca después de mergear** la rama
  `feat/ft-market-data-yfinance`, que extrae `finances_core.marketdata` (`hive_writer`, `inventory`, `plans`,
  `tasks_lifecycle`) y amplía `DatasetIdentifier.market_type`. Este plan asume ambos en `main`.
- `finances_core.export` recibe `stream_export` desde `ft_data_viewer` (el viewer pasa a importarlo; sus tests son la
  red de seguridad).
- `finances_core.marketdata.hive_writer` y `plans` ganan parámetros retrocompatibles (`file_name`, `mode`, `prefix`).
- `finances_api.create_app()` monta `/indicators`; `finances-api` añade la dependencia `finances-analytics`; `GET
  /health` puede reportar `indicators_dir_accessible` (spec de `finances_api`).
- `ft_setup`: `FT_INDICATORS_DIR` y `FT_INDICATORS_MAX_CONCURRENT` en `env_list.conf(.j2)` con
  `source_tool="ft_indicators"`; `ft_setup init` crea `INDICATORS_DIR` junto a los demás directorios de datos.
- CI (`.github/workflows/ci.yml`): instalación editable core → downloader → **analytics** → api; `finances-analytics`
  en el bucle de mypy/pytest; paso de benchmarks en `ubuntu-latest`. `.pre-commit-config.yaml`: hook
  `mypy-finances-analytics`.
- `ft_data_viewer` no cambia de comportamiento: `FT_INDICATORS_DIR` está fuera de `FT_PARQUET_DIR` (RF-7).
- Constitución: la excepción `Float64` interna (ADR-I11) se registra como RFC del Art. 5.3 antes del merge: `docs/rfc/RFC-0001-ft-indicators-float64-compute.md`.
- Futura spec GPU/backtesting: consumirá `fixtures/oracle/v1` y la tabla de tolerancias de §3.9 (contrato de
  validación de kernels `f32`).
