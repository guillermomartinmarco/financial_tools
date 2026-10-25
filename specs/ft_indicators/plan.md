# Plan — `ft_indicators` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.0 Núcleo compartido con `ft_ta_lib` (Art. 4)
`ft_indicators` y su herramienta hermana `ft_ta_lib` (`specs/ft_ta_lib/`) viven en el mismo paquete
`finances-analytics` pero **no pueden importarse entre sí**. Todo lo que ambas necesitan se construye en
`finances_core` en la Fase 1 de este plan, de forma genérica (parametrizado por **espacio de nombres** de
herramienta), y `ft_ta_lib` lo reutiliza sin cambios:

| Pieza compartida | Módulo `finances_core` | Usada por |
|---|---|---|
| Nomenclatura de series/columnas, contratos genéricos (`Stability`, manifiesto, progreso, resultado, despacho, exportación) | `schemas/indicators.py` | ambas |
| Gramática `name[:k=v,...]` y parseo de dataset `ex/mt/sym/tf` | `indicators_store/request.py`, `schemas/data_viewer.py` (`DatasetIdentifier.parse`) | ambas |
| Espacios de nombres (`ft_indicators` sin prefijo y sin `talib_`; `ft_ta_lib` con `talib_`) | `indicators_store/namespaces.py` | ambas |
| Carga y validación de la fuente Hive (Art. 13), columnas disponibles | `indicators_store/source.py` | ambas |
| Almacén por serie y mes bajo `FT_INDICATORS_DIR` (reescritura completa, huérfanos, lectura) | `indicators_store/store.py` | ambas |
| Manifiesto `_manifest.{namespace}.json` y bloqueo `_lock.{namespace}` | `indicators_store/manifest.py` | ambas |
| Vista ancha para exportar (rango, columnas, OHLCV, `stability`, desactualización) | `indicators_store/view.py` | ambas |
| Formatos `parquet`/`csv`/`jsonl` en streaming | `export.py` | ambas + `ft_data_viewer` |
| Orquestación de tareas `PENDING → PLANNING → COMPUTING → COMPLETED/FAILED` sobre `tasks_lifecycle` | `indicators_store/runner.py` | ambas |

Cada herramienta aporta solo su catálogo, su resolución de specs, su motor de cálculo y sus adaptadores CLI/API.

**API implementada en la Fase 1** (contrato que reutiliza `ft_ta_lib`; precisa lo esbozado en §1.1–§3.8):
- `DatasetIdentifier` es inmutable (`frozen=True`, *hashable*: clave de `items_by_dataset`); `parse()`/`path_segment()`.
- `indicators_store.request.parse_spec_token(token) -> ParsedSpec(name, params)`; `SpecTokenError(ValueError)`. Claves y
  valores identificador se normalizan a minúsculas; el nombre conserva mayúsculas (cada herramienta lo normaliza).
- `indicators_store.namespaces`: `NAMESPACES`, `validate_series_id`, `belongs_to`, `manifest_file_name`,
  `lock_file_name`; `NamespaceViolationError(ValueError)`. También se valida el namespace de cada **columna** escrita.
- `indicators_store.source`: `dataset_dir`, `source_files`, `dataset_exists`, `available_columns`,
  `load_source -> SourceFrame(dataset, frame, files)` (archivos en orden cronológico, **sin** reordenar filas: el orden
  lo verifica `validate_source`), `validate_source -> ValidatedSource(frame, null_counts)`;
  `SourceNotFoundError(LookupError, code="dataset_not_found")`, `InvalidSourceError(ValueError, code="invalid_source")`.
- `indicators_store.store.IndicatorStore(root, namespace, *, tasks_db_path=None)` + `IndicatorStore.from_settings(settings,
  namespace)` (aplica la validación RF-7); `write_series(...) -> WriteStats(rows, first_ms, last_ms, partitions_written,
  partitions_removed, files)`; `scan_series(ds, series_id, *, start_ms=None, end_ms=None)` (poda por año/mes, `end`
  exclusivo); `update_manifest(ds, entries)` (solo las entradas dadas); `series_files`, `has_series`, `lock`.
  Dtypes persistibles `Float32`/`Int16`/`Int64` (`Float64` → `SeriesDtypeError`); valores no enteros o fuera de `Int16`
  → `SeriesDtypeError` (Art. 5.1 bis: nunca se trunca); `SeriesNotFoundError(LookupError)`.
- `indicators_store.manifest`: `read_manifest`, `write_manifest`, `merge_entries`, `acquire_lock`/`release_lock`/
  `dataset_lock`, `task_liveness(tasks_db_path)`; `DatasetLockedError(owner_task_id)`. Al liberar un bloqueo de un
  dataset sin entradas (p. ej. fuente inexistente) se elimina el manifiesto en lugar de dejarlo vacío.
- `indicators_store.view.build_wide_view(store, ds, req, *, settings=None) -> WideView(frame, stale, series)`;
  `ExportValidationError(ValueError)` (rango invertido, `drop` sin `stable_from_ms`).
- `indicators_store.runner`: `create_compute_task(task_id, *, datasets_total, items_total, settings)`,
  `run_compute_task(..., on_progress=None, on_phase=None)`, `DatasetOutcome(items, entries, rows_computed, warnings)`,
  `get_progress`, `get_result`, `dispatch`. El runner toma el bloqueo, llama a `compute_dataset` (carga, valida,
  calcula y `write_series`) y escribe el manifiesto con `DatasetOutcome.entries`. Errores por dataset con prefijo
  `invalid_source:` / `dataset_not_found:` / `dataset_locked:`.
- `finances_core.export`: además `ForbiddenOutputDirError(ValueError)` lanzado por `validate_output_dir`.
- `schemas.indicators.DispatchResponse` es el mismo modelo que `schemas.dispatch.DispatchResponse` (re-exportado).

### 1.1 Esquemas (`packages/finances-core/src/finances_core/schemas/indicators.py`)
Viven en `finances_core` porque `ft_ta_lib` y el futuro backtester necesitan la nomenclatura y el manifiesto sin
importar `ft_indicators` (Art. 4).

**Genéricos (compartidos):**
```python
Namespace = Literal["ft_indicators", "ft_ta_lib"]
ComputePhase = Literal["PENDING", "PLANNING", "COMPUTING", "COMPLETED", "FAILED"]
PriceSource = Literal["open", "high", "low", "close", "adj_close"]

class Stability(BaseModel):                         # RF-16
    stable_from_ms: int | None                     # serie = máximo de sus columnas
    by_column: dict[str, int | None]
    rule: str                                      # p. ej. "ewm alpha=2/21 eps=1e-6 seed_idx=19 k=139"
    reason: Literal["converged", "first_non_null", "non_decaying_seed", "not_converged",
                    "unstable_period_unknown"]     # el último lo usa ft_ta_lib

class StoreEntry(BaseModel):                       # una serie persistida (un indicador / una función TA-Lib)
    series_id: str                                 # = indicator_id en ft_indicators
    spec: dict[str, object]                        # spec resuelta propia de la herramienta
    columns: list[str]
    column_dtypes: dict[str, str]                  # "Float32" | "Int16" | "Int64"
    engine_version: str                            # p. ej. "ft_indicators:3" o "ta-lib 0.8.2 / C 0.8.1"
    rows: int
    first_ms: int | None
    last_ms: int | None                            # detección de desactualización
    stability: Stability
    computed_at_ms: int

class StoreManifest(BaseModel):                    # GET /datasets/{...}
    namespace: Namespace
    dataset: DatasetIdentifier
    lock_task_id: str | None = None
    entries: dict[str, StoreEntry]

class ComputeProgress(BaseModel):
    task_id: str
    status: ComputePhase
    datasets_total: int
    datasets_done: int
    items_total: int
    items_done: int
    rows_computed: int
    partitions_written: int
    stability: dict[str, Stability]                # clave "ex/mt/sym/tf:series_id"
    warnings: list[str]
    errors: list[str]

class ComputeItemResult(BaseModel):
    dataset: DatasetIdentifier
    series_id: str
    columns: list[str]
    rows: int
    first_ms: int | None
    last_ms: int | None
    stability: Stability
    partitions_written: int
    partitions_removed: int
    warnings: list[str]                            # insufficient_history, not_converged, null_inputs=N

class ComputeResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    items: list[ComputeItemResult]
    output_dirs: list[str]
    warnings: list[str]
    errors: list[str]

class DispatchResponse(BaseModel):                 # mismo contrato que los descargadores
    task_id: str
    status: Literal["ACCEPTED"]
    poll_url: str
    plan_url: str
    result_url: str

class SeriesExportRequest(BaseModel):
    start_ms: int | None = None
    end_ms: int | None = None                      # exclusivo
    series: list[str] | None = None                # series_id; None = todas las del namespace
    columns: list[str] | None = None
    include_ohlcv: bool = False
    stability: Literal["all", "flag", "drop"] = "all"
    format: ExportFormat = "parquet"               # finances_core.export
    output_dir: str | None = None                  # None/"-" = stream
```
Nomenclatura genérica (RF-4): `format_param(value) -> str` (`.`→`p`, negativo → prefijo `m`, sin ceros sobrantes,
máx. 6 decimales), `series_id(prefix, name, param_tokens, input_overrides) -> str` (minúsculas, `[a-z0-9_]`),
`output_columns(series_id, outputs) -> list[str]`.

**Propios de `ft_indicators`** (en el mismo módulo, para el backtester):
```python
IndicatorName = Literal["sma", "ema", "wma", "rsi", "macd", "bb", "atr",
                        "stoch", "roc", "obv", "adx", "vwap"]

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

class IndicatorCatalogEntry(BaseModel):            # GET /catalog
    name: IndicatorName
    params: dict[str, dict[str, object]]           # {param: {type, default, min, max}}
    inputs: list[str]
    outputs: list[str]                             # sufijos de salida ([] si única)
    warmup: str
    stable_from: str                               # fórmula legible RF-16
    f32_tolerance: dict[str, dict[str, float]]     # §3.9
    talib_equivalent: TalibEquivalentInfo | None   # None para vwap; legible por máquina (validación cruzada, ft_ta_lib RF-18)
    engine_version: int
    convention: str
```
```python
class TalibEquivalentInfo(BaseModel):
    function: str                                  # p. ej. "BBANDS"
    params: dict[str, str]                         # parámetro TA-Lib -> expresión sobre params propios, p. ej. {"nbdevup": "std", "matype": "0"}
    outputs: dict[str, str]                        # sufijo propio -> salida TA-Lib, p. ej. {"upper": "upperband"}
    compare_from: Literal["first_non_null", "stable_from"] | int   # ajuste documentado (§3.11)
    excluded_rows: str | None = None               # p. ej. "roc base == 0"
```
`DatasetIdentifier` se reutiliza de `finances_core.schemas.data_viewer` (con `market_type` ampliado por la rama
`ft_market_data_yfinance`, ver §7) y gana `DatasetIdentifier.parse("exchange/market_type/symbol/timeframe")` y
`.path_segment()`.

### 1.2 Esquemas Polars
```python
SERIES_FILE_KEY = {"open_time_ms": pl.Int64}                # + columnas de salida (Float32 en ft_indicators)
EXPORT_ID_COLUMNS = ["exchange", "market_type", "symbol", "timeframe", "open_time_ms"]
OHLCV_EXPORT_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]   # adj_close si existe
```

### 1.3 Configuración (`finances_core/config.py`)
```python
INDICATORS_DIR: pathlib.Path = pathlib.Path("./data/indicators")   # resuelto desde PROJECT_ROOT; compartido con ft_ta_lib
INDICATORS_MAX_CONCURRENT: int = 2                                 # ge=1, le=16
```
Validación (RF-7): `INDICATORS_DIR` no puede estar dentro de `PARQUET_DIR` → `ConfigError`, comprobada al usarlo con
`FinancesSettings.require_indicators_dir()` (la usa `IndicatorStore.from_settings`), no al construir los settings: así una
configuración errónea de indicadores no rompe el resto de herramientas que importan `finances_core.config`.

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
- `--indicator` usa la gramática compartida `finances_core.indicators_store.request.parse_spec_token`
  (`name(':' key '=' value (',' key '=' value)*)?`) y produce `IndicatorSpec`; misma validación que la API.
- `--start/--end` aceptan lo mismo que `finances_core.timeutils.parse_time_input`.
- `compute` crea la tarea, ejecuta el servicio en proceso (`asyncio.run`) con barra Rich por dataset e imprime
  la tabla final; `--dry-run` imprime el `IndicatorPlan` sin crear tarea ni escribir.
- `export` sin `-o` o con `-o -` → stdout binario; el resumen va a stderr.

### 2.2 API (FastAPI) — `.../ft_indicators/api.py`, `router = APIRouter(prefix="/indicators")`
| Método | Ruta | Respuesta |
|---|---|---|
| GET | `/catalog` | `list[IndicatorCatalogEntry]` |
| POST | `/compute` (cuerpo `IndicatorComputeRequest`) | `202` `DispatchResponse` · 400 · 404 dataset · 409 bloqueado |
| GET | `/status/{task_id}` | `ComputeProgress` · 404 |
| GET | `/plan/{task_id}` | `IndicatorPlan` · 404 |
| GET | `/result/{task_id}` | `ComputeResult` · 404 · 409 si no terminó |
| GET | `/datasets/{exchange}/{market_type}/{symbol}/{timeframe}` | `StoreManifest` (`namespace="ft_indicators"`) · 404 |
| POST | `/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export` (cuerpo `SeriesExportRequest`) | `StreamingResponse` (`Content-Type` por formato, `Content-Disposition`, `X-FT-Indicators-Stale` opcional) · 400 · 404 |

- `task_id` validado con `^[0-9a-f-]{36}$`. Montado por `finances_api.create_app()`.
- `POST /compute`: valida, comprueba existencia de datasets (solo `stat` de directorios) y bloqueos, crea la tarea
  `PENDING` y encola la ejecución en `BackgroundTasks` (vía `indicators_store.runner`); la planificación ocurre dentro
  de la tarea (fase `PLANNING`) para responder en ≤ 200 ms.
- Exportación: se resuelve en `run_in_threadpool` a un directorio temporal y se sirve con `StreamingResponse`
  + `BackgroundTask` de limpieza (mismo patrón que `ft_data_viewer`).

## 3. Algoritmos Clave

### 3.1 Catálogo — `ft_indicators/catalog.py`
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
    stages: Callable[[ResolvedParams], Mapping[str, StageGraph]]   # RF-16
    f32_tolerance: Mapping[str, Tolerance]                         # §3.9
    talib: TalibEquivalent | None         # función, mapeo de parámetros y ajustes de test (§3.11)
    kernel: Kernel                        # §3.3
    engine_version: int
    convention: str

CATALOG: Mapping[IndicatorName, IndicatorDef]
def resolve(spec: IndicatorSpec, available_columns: set[str]) -> ResolvedIndicator   # defaults + RF-2/RF-6
```
`indicator_id` = `series_id(prefix="", name, params en orden, overrides)`; el espacio de nombres `ft_indicators`
rechaza ids con prefijo `talib_`.

**Implementado (Fase 2)**: `resolve(spec, available_columns=None)` (`None` = validación sin dataset) devuelve
`ResolvedIndicator(definition, params, indicator_id, columns: {sufijo: columna}, inputs)`; errores
`errors.IndicatorValidationError(indicator, param, value, valid)`. Los sufijos de salida usan `""` para la columna base
(`adx_14` junto a `adx_14_plus_di`); en las claves publicadas (`f32_tolerance`, `talib_equivalent.outputs`) la columna
base se nombra con el nombre del indicador. `IndicatorDef` añade `kind` (`recursive`/`windowed`/`non_decaying`) y
`cross_check` (`macd.fast < slow`); la convención de `adx` es la de TA-Lib (semilla de las sumas en `period−1`, §3.11); `TalibEquivalent.outputs` mapea a `"FUNCION:salida"` (ADX usa `ADX`, `PLUS_DI`,
`MINUS_DI`) y `compare_from` es función de los parámetros (publicado con los defaults: `stoch` → 17). Los enteros
aceptan reales integrales (`14.0` → `14`); `bool` se rechaza. La petición CLI vive en `request.py` (`spec_from_token`,
`build_request`, `load_request_file`, `dedupe_specs`).

### 3.2 Fuente — `finances_core.indicators_store.source` (compartido)
- `dataset_dir(settings, ds)` vía `finances_core.marketdata.inventory.partition_base_dir`; `dataset_exists(ds)`;
  `available_columns(ds) -> set[str]` (lee solo el esquema de un archivo).
- `load_source(settings, ds, columns) -> SourceFrame`: `pl.scan_parquet` de todos los `year=*/month=*/data.parquet`,
  solo `open_time_ms` + columnas pedidas, ordenado, `collect()` una vez.
- `validate_source(df, required)` (Art. 13): columnas y dtypes canónicos, `open_time_ms` estrictamente creciente y
  único; `NaN/inf` → null; devuelve conteo de nulos por columna. Error → `InvalidSourceError`.

### 3.3 Kernels — `ft_indicators/kernels/` (expresiones Polars puras, cálculo `Float64`)
Firma única (implementada): `kernel(program: Program, params, dtype=Float64) -> dict[sufijo, pl.Expr]`: expresiones
`Float64` alineadas 1:1 sobre las columnas de entrada. `kernels/common.Program` liga los intermedios reutilizados a
columnas temporales (`__ft_*`) que el motor añade por niveles de dependencia dentro de la misma consulta: sin ello los
árboles crecen exponencialmente (`ewm_seeded` referencia su entrada 4 veces; ADX encadena 3 niveles: 2,3 M caracteres y
~0,9 s solo de planificación). `kernels/common.evaluate(frame, kernel, params, dtype)` evalúa un kernel aislado (tests).
Las entradas `Float32` se convierten a `Float64` al entrar al kernel (`Program.float_input`, no finitos → nulo); el motor
(§3.4) convierte a `Float32` antes de persistir. `Float64` no sale de `kernels/` + `engine.py` salvo hacia el paquete del
oráculo (§3.10). `dtype=Float32` solo lo usa el test de alcanzabilidad de la tabla §3.9 (T2.9).
**División entre constantes**: Polars divide entre un escalar con resultado dependiente de la longitud del marco
(división real en marcos pequeños, multiplicación por el recíproco en el resto), lo que rompe el prefijo estable bit a
bit (RF-3); los kernels usan `scaled(x, n) = x · (1/n)`.
**Ventana de `roc`**: `[t−period, t]` (`period+1` filas): una fila nula en ella anula la salida (RF-3).

| Archivo | Indicadores | Técnica |
|---|---|---|
| `windowed.py` | `sma`, `wma`, `bb`, `roc`, `vwap`, `stoch` | `rolling_*`/`shift` exigiendo ventana completa (ventana con nulo → nulo); `bb` σ con `ddof=0`; `stoch` HH/LL con `rolling_max/min` |
| `recursive.py` | `ema`, `rsi`, `macd`, `atr`, `adx` | Semilla SMA insertada en la fila de semilla y `ewm_mean(alpha=…, adjust=False, ignore_nulls=True)` sobre la subsecuencia no nula; Wilder = α = 1/period; sumas de Wilder de ADX = media de Wilder × period |
| `cumulative.py` | `obv` | `sign(diff(close)) * volume` → `cum_sum` (primera fila = volume_0) |

Convenciones de división por cero (RF-2: RSI sin movimiento = 0 y Estocástico con HH = LL = 0, como TA-Lib) con
`pl.when(...).then(...)`.

### 3.4 Motor — `ft_indicators/engine.py` (recálculo completo)
- Una única consulta *lazy* sobre la serie completa: temporales del `Program` (`with_columns` por nivel) + `select`
  de todas las salidas, y el `cast` único a `Float32` (`PERSIST_DTYPE`) dentro de la misma consulta; Polars paraleliza
  columnas; determinista (misma entrada ⇒ misma salida), lo que da idempotencia por construcción (RF-8). API:
  `compute(frame, resolved) -> EngineResult(frame Float32, outputs{indicator_id: IndicatorOutput(stability, warnings,
  null_inputs)})` y `compute_float64(frame, resolved)` (solo oráculo y tests). `insufficient_history` ⇔ filas válidas ≤
  calentamiento de alguna columna; `null_inputs=N` = filas con alguna entrada nula o no finita.
- Garantías verificadas por tests: alineación 1:1 con la fuente, calentamiento nulo, sin anticipación (prefijo
  estable), cálculo `Float64` y salida `Float32` (`cast` único en la frontera).
- Tras calcular, `convergence.stability(def, params, non_null_index_map)` (§3.9) produce `Stability` por columna.
- Historia insuficiente ⇒ columna nula + `insufficient_history`.
- Memoria: 1M filas × (6 entradas + 19 salidas) × 8 B (`Float64` interno) ≈ 200 MB por dataset, más intermedios;
  aceptable con 2 datasets en paralelo.

### 3.5 Planificación — `ft_indicators/planner.py`
`plan_items(request, settings) -> IndicatorPlan` (puro, sin cálculo): resuelve specs, `indicator_id` y columnas;
verifica columnas disponibles (RF-6). Persiste con
`finances_core.marketdata.plans.save_plan(task_id, plan, prefix="indicators_plan")`.

### 3.6 Almacenamiento — `finances_core.indicators_store.store` / `.manifest` (compartido)
```python
class IndicatorStore:
    def __init__(self, root: Path, namespace: Namespace) -> None
    def dataset_dir(self, ds: DatasetIdentifier) -> Path
    def write_series(self, ds, series_id: str, df: pl.DataFrame, dtypes: Mapping[str, pl.DataType]) -> WriteStats
    def scan_series(self, ds, series_id: str) -> pl.LazyFrame
    def read_manifest(self, ds) -> StoreManifest | None
    def write_manifest(self, ds, manifest: StoreManifest) -> None
    def lock(self, ds, task_id: str) -> ContextManager[None]      # DatasetLockedError si hay dueño vivo
```
- `write_series`: valida que `series_id` respete el espacio de nombres; añade `year`/`month` y escribe con
  `finances_core.marketdata.hive_writer.write_parquet_partitioned(..., exchange=…, file_name=f"{series_id}.parquet",
  mode="replace")`: cada mes se reemplaza de forma atómica con todas sus filas; después borra los
  `{series_id}.parquet` de meses sin filas fuente. Los dtypes los decide la herramienta (`Float32` aquí; `ft_ta_lib`
  también `Int16`/`Int64`).
- **Por qué un archivo por serie** (y no una tabla ancha por dataset): con recálculo completo de solo lo pedido, una
  tabla ancha obligaría a reescribir columnas no pedidas (o dejarlas desalineadas) y a coordinar dos herramientas
  sobre el mismo archivo; con un archivo por serie cada una es autocontenida y el reemplazo atómico es por archivo.
  La tabla ancha la produce `export`.
- Manifiesto `_manifest.{namespace}.json` (validado con `StoreManifest`, escritura atómica `tmp` + `os.replace`),
  escrito **después** de las particiones; solo se actualizan las entradas pedidas.
- Bloqueo `_lock.{namespace}` creado con `O_EXCL` + `lock_task_id` en el manifiesto; obsoleto si la tarea dueña es
  terminal o no existe en `tasks.db` (RF-8). Las dos herramientas no se bloquean entre sí (archivos disjuntos).

### 3.7 Exportación — `finances_core.indicators_store.view` + `finances_core.export` (compartidos)
`build_wide_view(store, ds, req: SeriesExportRequest) -> (pl.LazyFrame, stale: list[str])`: base = `open_time_ms` de
la fuente en `[start, end)` (si `include_ohlcv`) o unión de los `open_time_ms` de las series; `join(how="left",
on="open_time_ms")` de cada serie (`scan_series` con poda Hive por año/mes); columnas identidad constantes.
`stale` = series cuyo `last_ms` es menor que el máximo `open_time_ms` de la fuente. `stability`: `flag` añade
`{series_id}_is_stable`; `drop` filtra `open_time_ms >= max(stable_from_ms)` (nulo ⇒ error de validación).
Streaming con `finances_core.export.stream_export(lf, fmt, output)`. `ft_indicators/exporter.py` solo traduce
`--indicator` a `series_id`.

### 3.8 Orquestador — `ft_indicators/service.py` sobre `finances_core.indicators_store.runner` (compartido)
```python
async def run_compute_task(task_id: str, plan: BaseModel, items_by_dataset: Mapping[DatasetIdentifier, Sequence[T]],
                           compute_dataset: Callable[[DatasetIdentifier, Sequence[T]], DatasetOutcome],
                           store: IndicatorStore, settings: FinancesSettings, *, plan_prefix: str,
                           on_progress: ProgressCallback | None = None) -> ComputeResult
def dispatch(task_id: str, base_url: str, prefix: str) -> DispatchResponse
```
Reusa `finances_core.marketdata.tasks_lifecycle` (`create_download_task`, `TaskRecorder` con
`phase_order=("PENDING","PLANNING","COMPUTING")`, `run_download_sync`, `ensure_finished`):
```
create_task (PENDING) → PLANNING (plan + save_plan) → COMPUTING (por dataset, semáforo FT_INDICATORS_MAX_CONCURRENT:
lock → load_source → validate → compute_dataset (motor de la herramienta) → write_series → manifest → unlock)
→ COMPLETED | FAILED (solo IO/inesperado)
```
Errores por dataset (`InvalidSourceError`, dataset desaparecido) → `errors` y continúa.
Funciones públicas de `service.py`: `dispatch_compute(request)`, `run_compute(task_id, request, *, on_progress)`,
`get_progress/get_plan/get_result(task_id)`, `get_manifest(ds)`, `export(ds, req, *, stream=None)`, `catalog()`.

### 3.9 Convergencia y política de precisión — `ft_indicators/convergence.py`, `precision.py`
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
| `adx_14` | ±DI: Wilder semilla 13 (convención TA-Lib) → 200; ADX: max(27, 200) + 187 | ±DI 200; ADX 387 |
| `macd_12_26_9` | fast α=2/13 semilla 11, k*=83 → 94; slow α=2/27 semilla 25, k*=180 → 205; signal α=0,2, k*=62 | line 205; signal/hist 267 |
| `obv` | acumulado, semilla volume₀ no decae | nulo (`non_decaying_seed`) |
| no recursivos | — | primera fila no nula |

**Precisión** (decisión): calcular en `Float64`, **persistir en `Float32`** (Art. 5).
- Calcular en `Float64` hace al oráculo independiente del redondeo `f32` (lo que se quiere medir en GPU) y evita
  la deriva de sumas móviles y acumulados largos en `f32`. Excepción del Art. 5.3: aislada en `kernels/` + `engine.py`
  (frontera única de `cast`), justificada aquí, en ADR-I11 y en `docs/rfc/RFC-0001-ft-indicators-float64-compute.md`.
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

La tabla vive en `precision.py` (fuente única) y se publica en `GET /catalog` y en el paquete del oráculo como
`{clave_salida: {"rtol", "atol", "atol_scaled"}}` (error admitido = `atol + atol_scaled·escala + rtol·|x|`).

### 3.10 Paquete de *fixtures* del oráculo — `ft_indicators/oracle.py` + `scripts/export_oracle_fixtures.py`
Herramienta de desarrollo (no es funcionalidad de usuario; no requiere CLI/API, Art. 1 no aplica), generada a partir
de `ft_indicators` (no de TA-Lib): `export_oracle_pack(out_dir: Path, version: str) -> Path`, ejecutada por el script
y versionada en `packages/finances-analytics/tests/ft_indicators/fixtures/oracle/v1/`:
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
**Implementado (Fase 3)**: casos `random_walk`, `flat`, `nulls_nan`, `short`, `extreme_low`, `extreme_high`,
`hh_eq_ll`; los 12 indicadores con defaults; `export_oracle_pack(out_dir, version, *, commit)` escribe
`{out_dir}/{version}`. CSV: `,`, cabecera, LF, nulo = vacío, `NaN`/`Infinity` literales y `%.17g` también en las
entradas `Float32` (recuperables exactamente con `float()`/`strtod`). `manifest.json` por caso e indicador: rutas,
columnas, `stable_from_index` (fila, o `null`), `stability_reason`, `warnings`; `files` con sha256. La regeneración
exige CSV byte a byte y Parquet por contenido (los bytes Parquet llevan metadatos del escritor).

### 3.11 Contraste con TA-Lib en CI — `tests/ft_indicators/test_talib_parity.py`
TA-Lib (`ta-lib`, ruedas binarias cp312 Windows/manylinux) es dependencia del extra `[dev]`; solo los **tests**
lo importan (test AST, T0.3). Para cada indicador con `talib` en su `IndicatorDef`, sobre series sin nulos
(paseo aleatorio 5.000 velas, serie plana, precios extremos):

| Comparación | Desde | Tolerancia |
|---|---|---|
| Kernel `Float64` vs TA-Lib (`double`) — convención idéntica (`sma`, `ema`, `wma`, `rsi`, `bb`, `atr`, `roc`, `obv`, `adx`/±DI) | primera fila no nula (mismo índice de calentamiento exigido) | `rtol=1e-9` |
| Kernel `Float64` vs TA-Lib — `macd` | `stable_from` (267) | `rtol=1e-9` + `atol=1e-6·escala` |
| Kernel `Float64` vs TA-Lib — `stoch` | `k+smooth_k+d−3` (17) | `rtol=1e-9` |
| Persistido `Float32` vs TA-Lib | `stable_from_ms` | tabla §3.9 |

Ajustes explícitos (no tolerancia): `macd` y `stoch` según la tabla; `roc` con base 0 (TA-Lib 0, nosotros nulo) se
excluye; máscaras de nulos comparadas solo en calentamiento. `vwap` no tiene equivalente (solo *fixtures* a mano).
**Decisión del Tech Lead (Fase 2) — ADX con la semilla de TA-Lib**: TA-Lib 0.8 siembra las sumas de Wilder de
`PLUS_DI`/`MINUS_DI`/`ADX` con la suma de los `period−1` primeros valores seguida de un paso de Wilder en `t=period`;
`ft_indicators` adopta esa convención (`kernels.common.wilder_sum_seeded`; RF-2 actualizado), de modo que la paridad
`Float64` rtol 1e-9 se cumple desde la primera fila no nula (error relativo medido ~2e-15). La hoja de StockCharts
(semilla = Σ de los `period` primeros) pasa a ser un ajuste de convención documentado: se compara desde `stable_from`
(±DI 200, ADX 387) con rtol 1e-6 sobre las 504 filas de la hoja.
Los ejemplos publicados de StockCharts/Wilder siguen como *fixtures* adicionales en `fixtures/reference/`.

## 4. Estructura de Archivos Objetivo
```
packages/finances-core/src/finances_core/
├── export.py                       # NUEVO: stream_export, ExportFormat, MEDIA_TYPES, validate_output_dir (de ft_data_viewer)
├── indicators_store/               # NUEVO, compartido con ft_ta_lib (§1.0)
│   ├── __init__.py  namespaces.py  request.py  source.py  store.py  manifest.py  view.py  runner.py
├── marketdata/hive_writer.py       # + file_name="data.parquet", mode="merge"|"replace" (retrocompatible)
├── marketdata/plans.py             # + prefix="download_plan" en plan_file_path/save_plan/load_plan
├── schemas/data_viewer.py          # DatasetIdentifier.parse / path_segment
└── schemas/indicators.py           # NUEVO: contratos genéricos + propios de ft_indicators + nomenclatura RF-4
packages/finances-analytics/
├── pyproject.toml
├── scripts/export_oracle_fixtures.py    # genera tests/ft_indicators/fixtures/oracle/v1 (§3.10)
├── src/finances_analytics/
│   ├── __init__.py  py.typed
│   └── ft_indicators/
│       ├── __init__.py  catalog.py  engine.py  planner.py  exporter.py  service.py  errors.py  api.py  cli.py
│       ├── request.py  convergence.py  precision.py  oracle.py
│       └── kernels/  __init__.py  common.py  windowed.py  recursive.py  cumulative.py
└── tests/ft_indicators/
    ├── conftest.py                 # FT_PROJECT_ROOT temporal + fábrica de datasets Hive sintéticos
    ├── fixtures/reference/*.csv    # ejemplos publicados (StockCharts / Wilder) y cálculos a mano
    ├── fixtures/oracle/v1/         # paquete del oráculo (§3.10)
    ├── test_talib_parity.py        # §3.11
    ├── test_*.py
    └── benchmarks/test_bench_indicators.py   # marcador `benchmark` (RF-14)
```

## 5. Dependencias (pyproject.toml — finances-analytics)
- Runtime: `finances-core`, `polars>=1.6`, `pydantic>=2.8`, `pydantic-settings>=2.3`, `typer>=0.12`,
  `fastapi>=0.111`, `structlog>=24.1`, `rich>=13.7`, `uuid6>=1.11`, `pyarrow>=16.0`. **Nunca** `finances-downloader`
  ni `pandas`. `ft_indicators` no importa `talib` (test AST).
- `dev`: `pytest>=8.2`, `pytest-asyncio>=0.23`, `httpx>=0.27`, `ruff>=0.5`, `mypy>=1.10`, `hypothesis>=6.100`
  (RF-3), **`ta-lib>=0.8.2,<0.9`** (contraste §3.11; se fija la versión menor porque sus valores son la referencia).
  Cuando se implemente `ft_ta_lib`, `ta-lib` pasa a dependencia de runtime del paquete (ver `specs/ft_ta_lib/plan.md`).
- mypy: override `ignore_missing_imports` para `talib.*` (sin stubs).
- Script: `ft_indicators = "finances_analytics.ft_indicators.cli:app"`.
- Configuración de ruff/mypy/pytest copiada de `finances-downloader` (+ marcador `benchmark` excluido por defecto
  con `addopts = "-m 'not benchmark'"`).

## 6. Decisiones Arquitectónicas Clave (ADR)
| ID | Decisión | Motivo |
|---|---|---|
| ADR-I01 | Nuevo paquete `finances-analytics`, dependiente solo de `finances-core` | Separa cómputo analítico de la ingesta; aloja también `ft_ta_lib` y el backtester futuro sin arrastrar `yfinance`/`httpx` |
| ADR-I02 | Implementaciones propias en Polars, sin TA-Lib/pandas en runtime | Art. 8; control total de convenciones; rol de oráculo independiente |
| ADR-I03 | Convenciones Wilder/TA-Lib (RSI, ATR, ADX, EMA con semilla SMA, casos planos = 0); MACD con EMAs de semilla propia (StockCharts) | Referencias publicadas y verificables; desviaciones documentadas y tratadas como ajustes de test |
| ADR-I04 | Recálculo completo por dataset en cada ejecución | Simple, determinista e idempotente por construcción; 1M filas cuestan segundos (RF-14). Incremental = mejora futura (No-Go) |
| ADR-I05 | Un archivo por serie y mes (`{series_id}.parquet`) con las mismas claves Hive que la fuente | Reescribir solo lo pedido; dos herramientas en el mismo directorio sin coordinarse |
| ADR-I06 | `close` crudo por defecto; `adj_close` opt-in | Coherencia con H/L crudos y con lo que se opera |
| ADR-I07 | VWAP móvil sí, VWAP de sesión no | Bien definido 24/7 y en cualquier timeframe; la sesión requiere calendario inexistente en el esquema |
| ADR-I08 | Petición canónica única (`IndicatorComputeRequest`) con gramática CLI y `--request-file` | CLI ≡ API verificable por igualdad de modelos (Art. 1) |
| ADR-I09 | Almacén, manifiesto, fuente, vista de exportación, runner, nomenclatura y formatos en `finances_core` | Art. 4: `ft_indicators` y `ft_ta_lib` (mismo paquete) no se importan; tampoco `ft_data_viewer`; el backtester no importa ninguna |
| ADR-I10 | TA-Lib como dependencia de test (`[dev]`) y contraste directo en CI | Ruedas binarias disponibles (cp312 Windows/manylinux); referencia independiente sin *fixtures* generados a mano |
| ADR-I11 | Cálculo interno `Float64`, persistencia `Float32` | Rol de oráculo sin violar el Art. 5 en datos persistidos; excepción del Art. 5.3 (RFC-0001) aislada en `kernels/`+`engine.py` |
| ADR-I12 | `ft_indicators` es el oráculo de referencia de los futuros kernels GPU `f32` | WGSL no tiene `f64`; RSI/Bollinger/MACD nunca serán bit a bit en `f32` (experimentos WGSL del Tech Lead): se valida con tolerancias por columna y paquete de *fixtures* estable |
| ADR-I13 | `stable_from_ms` analítico (ε = 1e-6) además de los nulos de calentamiento | La semilla de las EMA contamina ~160 velas (EMA 20: 158); el consumidor decide descartarlas sin perder la alineación 1:1 |
| ADR-I14 | Espacios de nombres por herramienta (`talib_` reservado), manifiesto y bloqueo por herramienta | `ft_indicators` y `ft_ta_lib` comparten `FT_INDICATORS_DIR` sin colisiones de archivos, columnas ni bloqueos |

## 7. Integración con `finances-core` y la suite
- **Dependencia de orden**: la rama `feat/ft-indicators` arranca con `feat/ft-market-data-yfinance` ya mergeada
  (extracción de `finances_core.marketdata` y `DatasetIdentifier.market_type` ampliado). `ft_ta_lib` se implementa
  **después** de `ft_indicators` y reutiliza el núcleo de §1.0 sin cambios de contrato.
- `finances_core.export` recibe `stream_export` desde `ft_data_viewer` (el viewer pasa a importarlo; sus tests son la
  red de seguridad).
- `finances_core.indicators_store` (§1.0) se diseña y prueba con dos espacios de nombres desde el principio (tests con
  un namespace `ft_ta_lib` sintético), para que `ft_ta_lib` no requiera cambios en el núcleo.
- `finances_core.marketdata.hive_writer` y `plans` ganan parámetros retrocompatibles (`file_name`, `mode`, `prefix`).
- `finances_api.create_app()` monta `/indicators`; `finances-api` añade la dependencia `finances-analytics`; `GET
  /health` puede reportar `indicators_dir_accessible` (spec de `finances_api`).
- `ft_setup`: `FT_INDICATORS_DIR` y `FT_INDICATORS_MAX_CONCURRENT` en `env_list.conf(.j2)` con
  `source_tool="ft_indicators"`; `ft_setup init` crea `INDICATORS_DIR` junto a los demás directorios de datos.
- CI (`.github/workflows/ci.yml`): instalación editable core → downloader → **analytics `[dev]`** (incluye `ta-lib`)
  → api; `finances-analytics` en el bucle de mypy/pytest; paso de benchmarks en `ubuntu-latest`.
  `.pre-commit-config.yaml`: hook `mypy-finances-analytics`.
- `ft_data_viewer` no cambia de comportamiento: `FT_INDICATORS_DIR` está fuera de `FT_PARQUET_DIR` (RF-7).
- Constitución: la excepción `Float64` interna (ADR-I11) se registra en `docs/rfc/RFC-0001-ft-indicators-float64-compute.md`
  antes del merge.
- Futura spec GPU/backtesting: consumirá `fixtures/oracle/v1` y la tabla de tolerancias de §3.9 (contrato de
  validación de kernels `f32`).
