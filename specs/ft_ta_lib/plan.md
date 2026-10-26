# Plan — `ft_ta_lib` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquemas (`packages/finances-core/src/finances_core/schemas/ta_lib.py`)
Se reutilizan los contratos genéricos de `finances_core.schemas.indicators` (definidos en `ft_indicators` plan §1.1):
`Stability`, `StoreEntry`, `StoreManifest` (`namespace="ft_ta_lib"`), `ComputeProgress`, `ComputeItemResult`,
`ComputeResult`, `DispatchResponse`, `SeriesExportRequest`, y la nomenclatura `format_param` / `series_id` /
`output_columns`. Propios:
```python
TaLibOutputKind = Literal["real", "pattern", "category", "index"]
MA_TYPE_NAMES = ("sma", "ema", "wma", "dema", "tema", "trima", "kama", "mama", "t3",
                 "hma", "disabled", "default", "zlema", "rma")   # == talib.MA_Type 0..13 (TA-Lib C 0.8.1)

class TaLibSpec(BaseModel):
    function: str                                  # nombre TA-Lib, sin distinción de mayúsculas (normalizado a MAYÚSCULAS)
    params: dict[str, StrictInt | StrictFloat | StrictStr] = {}   # str solo para *matype; sin bool
    inputs: dict[str, PriceSource | Literal["volume"]] = {}   # solo price/price0/price1

class TaLibComputeRequest(BaseModel):
    datasets: list[DatasetIdentifier] = Field(min_length=1, max_length=100)
    functions: list[TaLibSpec] = []
    groups: list[str] = []                         # al menos uno de functions/groups; ≤ 200 tras expandir

class TaLibPlanItem(BaseModel):
    dataset: DatasetIdentifier
    series_id: str
    spec: TaLibSpec                                # con defaults resueltos
    columns: dict[str, ColumnDtype]                # columna -> dtype persistido
    lookback: int

class TaLibPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: list[TaLibPlanItem]

class TaLibParamInfo(BaseModel):
    name: str
    type: Literal["int", "float", "matype"]
    default: int | float | str
    min: float | None = None
    max: float | None = None

class TaLibOutputInfo(BaseModel):
    name: str                                      # nombre TA-Lib (p. ej. "upperband")
    kind: TaLibOutputKind
    dtype: Literal["Float32", "Int16", "Int64"]

class TaLibCatalogEntry(BaseModel):                # GET /catalog
    function: str
    group: str
    display_name: str
    flags: list[str]                               # function_flags de TA-Lib
    inputs: dict[str, list[str]]                   # nombre de entrada TA-Lib -> columnas por defecto
    params: list[TaLibParamInfo]
    outputs: list[TaLibOutputInfo]
    default_lookback: int
    stability_rule: Literal["first_non_null", "unstable_period_unknown"]
    supported: bool
    unsupported_reason: Literal["unavailable_input", "non_causal"] | None = None

class TaLibCatalog(BaseModel):
    talib_version: str                             # wrapper, p. ej. "0.8.2"
    ta_lib_c_version: str                          # p. ej. "0.8.1"
    groups: dict[str, list[str]]
    functions: list[TaLibCatalogEntry]
```

> **Deriva (implementación T1.1/T2.2, 2026-10-25)**: TA-Lib C 0.8.1 publica 14 tipos de media
> (`talib.MA_Type` 0–13: los 9 clásicos + `hma`, `disabled`, `default`, `zlema`, `rma`) y `KDJ` usa
> `rma` (13) por defecto; `MA_TYPE_NAMES` los replica todos (posición = entero) y un test de `ft_ta_lib`
> verifica que coincide con la versión instalada. RF-4 ("0–8") queda ampliado a 0–13. El catálogo
> de esta versión tiene **201** funciones (no ≈158) y 200 soportadas (todas salvo `MAVP`).

### 1.2 Configuración
Sin variables nuevas: `INDICATORS_DIR`, `INDICATORS_MAX_CONCURRENT`, `PARQUET_DIR`, `SHARE_DIR`, `TASKS_DB_PATH`.

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-analytics/src/finances_analytics/ft_ta_lib/cli.py`
```
ft_ta_lib [-v] [-q] [--log-format rich|json] COMMAND
  catalog   [--group G] [--function F]          # JSON == GET /catalog
  compute   -d/--dataset EX/MT/SYM/TF... [-f/--function NAME[:k=v,...]...] [-g/--group G...] [--dry-run]
            | --request-file FILE.json [--dry-run]
  plan      TASK_ID                             # JSON == GET /plan/{task_id}
  status    TASK_ID                             # JSON == GET /status/{task_id}
  result    TASK_ID                             # JSON == GET /result/{task_id}
  show      EX/MT/SYM/TF                        # JSON == GET /datasets/{...}
  export    EX/MT/SYM/TF [-f/--function ID_O_SPEC...] [--column COL...]
            [--start T] [--end T] [--include-ohlcv] [--stability all|flag|drop]
            [--format parquet|csv|jsonl] [-o/--output-dir DIR|-]
Exit: 0 OK · 1 no encontrado/bloqueado/genérico · 2 validación · 3 tarea FAILED o export vacío · 4 IO
```
- `--function` usa `finances_core.indicators_store.request.parse_spec_token`; las claves que coinciden con un nombre
  de entrada de la función (`price`, `price0`, `price1`) van a `TaLibSpec.inputs`, el resto a `params`.
- `--start/--end` con `finances_core.timeutils.parse_time_input`; `export` a stdout por defecto (resumen en stderr).

### 2.2 API (FastAPI) — `.../ft_ta_lib/api.py`, `router = APIRouter(prefix="/ta-lib")`
| Método | Ruta | Respuesta |
|---|---|---|
| GET | `/catalog` (query `group`, `function`) | `TaLibCatalog` |
| POST | `/compute` (cuerpo `TaLibComputeRequest`) | `202` `DispatchResponse` · 400 · 404 dataset · 409 bloqueado |
| GET | `/status/{task_id}` | `ComputeProgress` · 404 |
| GET | `/plan/{task_id}` | `TaLibPlan` · 404 |
| GET | `/result/{task_id}` | `ComputeResult` · 404 · 409 si no terminó |
| GET | `/datasets/{exchange}/{market_type}/{symbol}/{timeframe}` | `StoreManifest` (`namespace="ft_ta_lib"`) · 404 |
| POST | `/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export` (cuerpo `SeriesExportRequest`) | `StreamingResponse` (`X-FT-Indicators-Stale` opcional) · 400 · 404 |

`POST /compute` valida (incluida la ejecución de prueba de RF-4 sobre una serie sintética mínima, en *threadpool*),
comprueba datasets y bloqueos, crea la tarea `PENDING` y despacha con `finances_core.indicators_store.runner.dispatch`.

## 3. Algoritmos Clave

### 3.1 Catálogo — `ft_ta_lib/catalog.py`
- `build_catalog() -> TaLibCatalog` (con `functools.cache`): recorre `talib.get_function_groups()`; para cada nombre
  lee `talib.abstract.Function(name).info` (`group`, `display_name`, `function_flags`, `input_names`, `parameters`,
  `output_names`, `output_flags`) y `Function(name).lookback` con defaults.
- **Tipo de parámetro**: `matype` si el nombre termina en `matype`; si no, por el tipo del default (`int`/`float`).
  **Rango**: ta-lib 0.8.2 **no** expone el rango de `TA_GetOptInputParameterInfo` (su
  `_ta_getOptInputParameterInfo` privado solo devuelve nombre, tipo, ayuda y default; verificado en T2.2), así que
  `min`/`max` son `None` y la validación de rango recae en la ejecución de prueba (`TA_BAD_PARAM`, *lookback* `-1`).
- **Tipo de salida** (`TaLibOutputKind`): salida `double` → `real`; entera en el grupo *Pattern Recognition* →
  `pattern`; entera en `INDEX_FUNCTIONS = {"MAXINDEX", "MININDEX", "MINMAXINDEX"}` → `index`; otra entera → `category`.
  El tipo entero se detecta por el `dtype` del array de salida en la ejecución de prueba sobre la serie sintética.
- **Soporte**: entrada `periods` u otra no mapeable → `unavailable_input`; lista `NON_CAUSAL` (vacía salvo que T3.2
  encuentre alguna) → `non_causal`.
- **Estabilidad**: *flag* `"Function has an unstable period"` → `unstable_period_unknown`; si no → `first_non_null`.

### 3.2 Resolución y validación — `ft_ta_lib/resolver.py`
`resolve(spec, available_columns) -> ResolvedFunction`: normaliza nombre, completa defaults en el orden de TA-Lib,
convierte `matype` nombre↔entero, valida tipos y rangos, mapea entradas (RF-3), calcula `series_id`
(`series_id("talib_", función.lower(), tokens, overrides)`) y columnas/dtypes. `probe(resolved)`: ejecuta la función
sobre una serie sintética de `max(64, 2·lookback)` filas para capturar `TA_BAD_PARAM` y fijar los dtypes de salida.

### 3.3 Motor — `ft_ta_lib/engine.py` (único módulo que importa `talib`)
```python
def compute_function(src: pl.DataFrame, fn: ResolvedFunction) -> FunctionResult:   # frame + Stability + warnings
    # 1. máscara de filas con todas las entradas no nulas/finitas; subsecuencia
    # 2. entradas -> np.ndarray float64 contiguo (frontera Float64, RFC-0002)
    # 3. talib.abstract.Function(name)(inputs_dict, **params) -> arrays
    # 4. real: cast Float32 (único punto) y NaN/±inf (incl. desbordamiento Float32) -> null
    #    | pattern/category: null en filas < lookback, comprobar rango, cast Int16
    #    | index: posición -> open_time_ms de la subsecuencia (Int64)
    # 5. re-alinear a todas las filas fuente (nulos en filas excluidas)
```
- Una función a la vez por dataset (memoria acotada: entradas `float64` ≈ 40 MB por 1M filas + salidas de una
  función); cada serie se escribe en cuanto se calcula.
- TA-Lib C es reentrante; la configuración global no se toca (test que verifica *unstable period* = 0 para todas las
  funciones al inicio y al final de una tarea).
- Paralelismo entre datasets por el runner compartido (`asyncio.to_thread` + `FT_INDICATORS_MAX_CONCURRENT`).

### 3.4 Planificación, almacenamiento, exportación y tareas (compartidos)
- `planner.py`: expande grupos (solo funciones `supported`), deduplica por `series_id`, persiste con
  `save_plan(task_id, plan, prefix="ta_lib_plan")`.
- Almacén: `finances_core.indicators_store.IndicatorStore(settings.INDICATORS_DIR, "ft_ta_lib")` con dtypes por
  columna (`Float32`/`Int16`/`Int64`); manifiesto `_manifest.ft_ta_lib.json`, bloqueo `_lock.ft_ta_lib`;
  `engine_version = f"ta-lib {talib.__version__} / C {ta_lib_c_version}"`.
- Exportación: `build_wide_view` + `stream_export`; `exporter.py` solo traduce `--function` a `series_id`.
- Orquestación: `run_compute_task(..., plan_prefix="ta_lib_plan")` con `compute_dataset` = bucle de
  `compute_function` + `write_series` + `Stability` (RF-17).

### 3.5 Validación cruzada con `ft_indicators` — `packages/finances-api/tests/test_cross_validation_indicators.py`
Ubicación: `finances-api` es la raíz de composición que ya monta ambos routers; el test usa solo HTTP
(`TestClient(create_app())`), así ninguna herramienta importa a la otra (Art. 4) y se prueba el sistema montado.
1. Fábrica de dataset sintético Hive (paseo aleatorio 5.000 velas) en un `FT_PROJECT_ROOT` temporal.
2. `GET /indicators/catalog` → para cada indicador con `talib_equivalent`, construir la spec de `/ta-lib` con su
   mapeo de parámetros y salidas.
3. `POST /indicators/compute` y `POST /ta-lib/compute`, *polling* hasta `COMPLETED`; exportar ambos en Parquet.
4. Comparar columnas `Float32` con la tabla de tolerancias publicada en el catálogo de `ft_indicators`, desde
   `compare_from`/`stable_from_ms`, excluyendo las filas declaradas (`excluded_rows`).

> **Deriva (implementación Fases 4–7, 2026-10-25)**:
> - **Errores por item (RF-11)**: el runner compartido (`finances_core.indicators_store.runner`) solo transporta
>   errores por dataset (`DatasetOutcome` no tiene `errors`). `service._ItemErrors` acumula los errores por item
>   (`talib_error: {dataset}: {series_id}: …`, `output_out_of_range: …`) desde los hilos de cálculo y los fusiona en
>   `errors_json` de `tasks.db` con cada progreso que publica el runner (tras registrar cada dataset y tras sus propias
>   escrituras de `errors_json`), de modo que `status`/`result` los muestran antes de `COMPLETED`. Mejora propuesta
>   (fuera de esta rama, Art. 15): `DatasetOutcome.errors` en el núcleo y retirar `_ItemErrors`.
> - `compute_dataset(ds, items, ctx: ComputeContext)` agrupa almacén, *settings*, `task_id` y errores por item.
> - El plan resuelve cada función **por dataset** (`resolve(spec, columnas_disponibles)`) para validar `adj_close`;
>   `expand` (con la ejecución de prueba) corre una sola vez por petición.
> - `GET /catalog?group=&function=` (y `ft_ta_lib catalog --group/--function`): filtro sin distinción de mayúsculas;
>   `groups` solo lista los grupos/funciones devueltos; grupo o función inexistente → 400 / exit 2 con los grupos válidos.
> - Rutas con nombre `ta_lib_{catalog,compute,status,plan,result,manifest,export}` (únicos en la app compuesta;
>   `url_for` resuelve las URLs de *polling* hacia `/ta-lib`). Cabecera de exportación desactualizada:
>   `X-FT-Indicators-Stale` (la misma que `ft_indicators`, vista compartida).
> - Nombre de exportación: `{exchange}_{market_type}_{symbol}_{timeframe}_ta_lib.{ext}` (no colisiona con
>   `…_indicators.{ext}` en un mismo directorio).
> - `BBANDS` tiene `timeperiod=20` por defecto en TA-Lib C 0.8.1: el `series_id` por defecto es
>   `talib_bbands_20_2_2_sma` (el ejemplo de RF-9 `talib_bbands_5_2_2_sma_upperband` corresponde a `timeperiod=5`).
> - Benchmarks (RF-15): `tests/ft_ta_lib/benchmarks/{test_bench_ta_lib.py,thresholds.json}`; B1 usa las 13
>   funciones equivalentes con los **defaults de TA-Lib**; el generador de 1M velas pasa a `tests/datasets.py`
>   (compartido con `ft_indicators`). En CI corre en `ubuntu-24.04` (imagen fija, no `ubuntu-latest`) con
>   `BENCH_ENFORCE: "0"` hasta la calibración de T5.2.
> - Validación cruzada (§3.5): la escala de `atol_scaled` (media de `|close|`, `Σ|volume|` en `obv`) no es legible por
>   máquina en el catálogo de `ft_indicators`; el test la fija según su plan §3.9 y añade 4 ULP de `Float32`
>   relativos a la escala de la columna (`ft_ta_lib` persiste TA-Lib redondeado a `Float32`).

## 4. Estructura de Archivos Objetivo
```
packages/finances-core/src/finances_core/schemas/ta_lib.py        # NUEVO: contratos §1.1
packages/finances-analytics/src/finances_analytics/ft_ta_lib/
├── __init__.py  model.py  catalog.py  resolver.py  request.py  engine.py  planner.py  exporter.py  service.py
├── errors.py  api.py  cli.py                                      # model.py: metadatos/función resuelta sin talib
packages/finances-analytics/tests/ft_ta_lib/
├── conftest.py  test_*.py
└── benchmarks/test_bench_ta_lib.py  thresholds.json               # marcador `benchmark` (RF-15)
packages/finances-api/tests/test_cross_validation_indicators.py   # RF-18
```

## 5. Dependencias
- `finances-analytics`: `ta-lib>=0.8.2,<0.9` pasa de `[dev]` a **runtime** (ruedas binarias cp312 Windows/manylinux
  x86_64); `numpy` explícito (frontera de `engine.py`). Script `ft_ta_lib = "finances_analytics.ft_ta_lib.cli:app"`.
  mypy: override `talib.*` (`ignore_missing_imports` + `follow_imports = "skip"`: sin stubs, se trata como `Any`).
- `ft_indicators` sigue sin importar `talib` (su test AST se mantiene); solo `ft_ta_lib/engine.py` y
  `ft_ta_lib/catalog.py`/`resolver.py` (metadatos) lo importan en `src/`.
- `finances-api`: sin cambios de dependencias (ya depende de `finances-analytics`).

## 6. Decisiones Arquitectónicas Clave (ADR)
| ID | Decisión | Motivo |
|---|---|---|
| ADR-T01 | Herramienta hermana en `finances-analytics`, sin importar `ft_indicators` | Art. 4; todo lo común vive en `finances_core` (almacén, manifiesto, vista, runner, nomenclatura) |
| ADR-T02 | Catálogo generado en ejecución desde `talib.abstract` | Funciones nuevas sin cambios de código; una sola fuente de verdad |
| ADR-T03 | Prefijo `talib_` + espacio de nombres propio (manifiesto y bloqueo) | Comparte `FT_INDICATORS_DIR` con `ft_indicators` sin colisiones |
| ADR-T04 | Parámetros completos en el `series_id`, `matype` por nombre, salidas con su nombre TA-Lib | Determinismo y legibilidad; mismo criterio que `ft_indicators` |
| ADR-T05 | `Float32` para reales; `Int16` para patrones/categóricas; `Int64` ms para índices | `Int8` no cabe (±200); `Int16` preserva el valor exacto de TA-Lib; índices crudos no son unibles |
| ADR-T06 | Frontera `float64` en `engine.py` (TA-Lib solo opera en `double`) + `Int16`, ambos bajo RFC-0002 | Art. 5.1/5.3/5.4: excepciones explícitas y aisladas |
| ADR-T07 | Subsecuencia sin nulos en lugar de propagar `NaN` | Un `NaN` envenenaría toda la recursión restante en TA-Lib |
| ADR-T08 | Validación cruzada en `finances-api` solo por HTTP | Único lugar que ve ambas herramientas sin violar el Art. 4 |
| ADR-T09 | `stable_from` = primera fila no nula o `unstable_period_unknown` | TA-Lib no da regla analítica; no se inventa una |

## 7. Integración y Orden
- **Dependencia de orden**: se implementa **después** de mergear `ft_indicators`; reutiliza sin cambios
  `finances_core.indicators_store`, `finances_core.export`, `finances_core.schemas.indicators` y el runner. Si algo
  del núcleo falta, se cambia primero en `specs/ft_indicators/plan.md` §1.0 (Art. 15).
- `finances_api.create_app()` monta `/ta-lib` (spec de `finances_api`).
- `ft_setup`: sin variables nuevas; la descripción de `FT_INDICATORS_DIR`/`FT_INDICATORS_MAX_CONCURRENT` en
  `env_list.conf(.j2)` menciona que son compartidas con `ft_ta_lib`.
- CI: sin cambios de instalación (ya instala `finances-analytics[dev]`); paso de benchmarks propio de `ft_ta_lib`
  (B1–B3, `ubuntu-24.04`, `BENCH_ENFORCE: "0"` hasta calibrar) y artefacto `bench-ft_ta_lib-<sha>`.
- README: mapa de utilidades y ejemplos de `ft_ta_lib catalog`/`compute`/`export`.
- Constitución: `docs/rfc/RFC-0002-ft-ta-lib-int16-float64-boundary.md` (Propuesto) debe aprobarse antes del merge.
