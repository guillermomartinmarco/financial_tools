# Plan — `ft_datasets` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Receta (`packages/finances-core/src/finances_core/schemas/datasets.py`)
```python
RecipeName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
Exchange = Literal["binance", "yfinance"]
YF_REPRODUCIBLE_TIMEFRAMES = ("1d", "1w", "1M")          # RF-2: sin intradía de Yahoo

class RecipeBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    exchange: Exchange
    market_type: str                                      # binance: spot|futures; yfinance: MARKET_TYPES - {"other"}
    symbols: list[str] = Field(min_length=1, max_length=100)
    timeframes: list[str] = Field(min_length=1)
    start: date                                           # 00:00 UTC
    end: date                                             # 00:00 UTC, exclusivo; start < end
    # validadores: market_type/timeframes por exchange (RF-2), start < end, sin duplicados

class DatasetRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    name: RecipeName
    description: str = ""
    datasets: list[RecipeBlock] = Field(min_length=1)
    def items(self) -> list[RecipeItem]                   # expansión cartesiana, orden estable
    def canonical_sha256(self) -> str                     # §3.3

class RecipeItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    exchange: Exchange
    market_type: str
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int                                           # exclusivo
    @property
    def key(self) -> str          # "binance/spot/BTCUSDT/1m@2024-01-01..2024-04-01"
    def dataset(self) -> DatasetIdentifier
```
La regla de "rango asentado" (`end ≤ hoy − FT_DATASETS_SETTLE_DAYS`) depende del reloj y de la configuración, así que
no vive en el modelo: la aplica `recipes.validate_reproducible(recipe, today, settle_days)` (§3.1).

### 1.2 *Lock* (`schemas/datasets.py`)
```python
HASH_ALGORITHM = "ftds-v1"

class Probe(BaseModel):
    row: int                       # índice 0, n//2, n-1
    open_time_ms: int
    close_bits: str                # "0x42f6e979" (patrón float32 exacto)
    close: float                   # legible; no participa en comparaciones

class ItemLock(BaseModel):
    key: str
    exchange: Exchange; market_type: str; symbol: str; timeframe: str
    start_ms: int; end_ms: int
    row_count: int = Field(ge=1)
    first_open_time_ms: int; last_open_time_ms: int
    strict_columns: list[str]
    content_hash: str                          # "sha256:<hex>" sobre las columnas estrictas
    column_hashes: dict[str, str]              # estrictas + informativas
    month_hashes: dict[str, str]               # "YYYY-MM" -> hash estricto del mes
    informational: dict[str, str]              # p. ej. {"adj_close": "sha256:..."}
    probes: list[Probe]                        # 3 sondas (RF-6)
    missing_candles: int | None                # solo binance (informativo)

class RecipeLock(BaseModel):
    lock_version: Literal[1]
    hash_algorithm: Literal["ftds-v1"]
    recipe: RecipeName
    recipe_sha256: str
    locked_at: datetime                        # informativo
    tool_versions: dict[str, str]              # informativo: finances-downloader, polars, yfinance
    items: list[ItemLock]                      # orden = DatasetRecipe.items()
```

### 1.3 Verificación, bloqueo y tarea
```python
ItemStatus = Literal["OK", "OK_WITH_WARNINGS", "MISSING", "MISMATCH", "UNLOCKED"]
VerifyStatus = Literal["OK", "MISMATCH", "UNLOCKED", "RECIPE_CHANGED"]
Drift = Literal["ROWS_CHANGED", "SCALE_RESTATED", "VALUES_CHANGED"]

class ItemVerification(BaseModel):
    key: str
    status: ItemStatus
    row_count: int
    expected_row_count: int | None
    content_hash: str | None
    expected_content_hash: str | None
    drift: Drift | None = None
    scale_factor: float | None = None          # solo SCALE_RESTATED
    columns_changed: list[str] = []
    months_changed: list[str] = []
    warnings: list[str] = []
    action: str | None = None                  # texto de RF-6

class VerifyReport(BaseModel):
    recipe: str
    status: VerifyStatus
    recipe_changed: bool
    items: list[ItemVerification]
    duration_ms: int
    def to_markdown(self) -> str               # tabla para $GITHUB_STEP_SUMMARY

class LockResult(BaseModel):
    recipe: str
    outcome: Literal["WRITTEN", "UNCHANGED", "UPDATED", "REJECTED"]
    lock_path: str
    differences: list[str]                     # claves/campos que difieren del lock previo

MaterializePhase = Literal["PENDING", "VALIDATING", "PRECHECKING", "DOWNLOADING",
                           "VERIFYING", "COMPLETED", "FAILED"]

class DownloadBlock(BaseModel):                # una invocación de CLI del descargador
    exchange: Exchange
    market_type: str
    symbols: list[str]
    timeframes: list[str]
    start_ms: int
    end_ms_inclusive: int                      # end_ms - 1
    argv: list[str]                            # comando exacto (sin rutas absolutas del ejecutable)

class MaterializePlan(BaseModel):
    task_id: str
    created_at_ms: int
    recipe: str
    skipped_items: list[str]                   # OK en la pre-verificación
    refetch_items: list[str]
    blocks: list[DownloadBlock]

class BlockRun(BaseModel):
    block: int
    exit_code: int | None                      # None = no se pudo lanzar / timeout
    duration_ms: int
    timed_out: bool
    stderr_tail: list[str]                     # últimas 50 líneas
    downloader_task_id: str | None

class MaterializeProgress(BaseModel):
    task_id: str
    status: MaterializePhase
    recipe: str
    items_total: int
    items_skipped: int
    blocks_total: int
    blocks_done: int
    errors: list[str]

class MaterializeResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    outcome: VerifyStatus | Literal["NOT_VERIFIED", "DOWNLOAD_FAILED"]
    recipe: str
    blocks: list[BlockRun]
    verify: VerifyReport | None
    errors: list[str]
```
`DispatchResponse` es el de `finances_core/schemas/dispatch.py`.

### 1.4 Configuración (`finances_core/config.py`)
```python
DATASETS_RECIPES_DIR: pathlib.Path = pathlib.Path("./datasets")
DATASETS_DIR: pathlib.Path = pathlib.Path("./data/datasets")
DATASETS_DOWNLOAD_TIMEOUT_S: int = Field(default=3600, ge=60)
DATASETS_SETTLE_DAYS: int = Field(default=7, ge=1)
def require_datasets_dir(self) -> pathlib.Path    # ConfigError si anida con PARQUET_DIR (RF-11)
```

### 1.5 Formato de receta (TOML) y receta inicial `datasets/benchmark-v1.toml`
```toml
schema_version = 1
name = "benchmark-v1"
description = "Datos reales para tests masivos: cripto 24/7 intradía y multi-activo diario."

[[datasets]]                       # 2 × 2 ítems: 1m = 131.040 filas/símbolo, 1h = 2.184
exchange = "binance"
market_type = "spot"
symbols = ["BTCUSDT", "ETHUSDT"]
timeframes = ["1m", "1h"]
start = 2024-01-01
end = 2024-04-01                   # exclusivo: Q1 2024 (incluye 29-feb)

[[datasets]]
exchange = "yfinance"
market_type = "equity"
symbols = ["AAPL"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01

[[datasets]]
exchange = "yfinance"
market_type = "index"
symbols = ["^GSPC"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01

[[datasets]]
exchange = "yfinance"
market_type = "fx"
symbols = ["EURUSD=X"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01
```
Elección: Q1 2024 es anterior al cambio de Binance Public Data a microsegundos en spot (2025), está cubierto por
archivos mensuales (fuente única y determinista: sin REST) y cabe holgado en la caché de GitHub (estimado < 20 MB).
Yahoo `1d` 2015–2024 (~2.500 filas por símbolo) incluye el *split* 4:1 de AAPL de 2020 (ya re-expresado) y no
depende de límites de historial.

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-downloader/src/finances_downloader/ft_datasets/cli.py`
```
ft_datasets [-v] [-q] [--log-format rich|json] COMMAND
  list                                   # recetas + estado del lock (sin verificar datos)
  show NAME                              # receta expandida (ítems, claves) + lock si existe (JSON)
  materialize NAME [--no-verify] [--refetch] [--dry-run]
                                         # tarea en proceso con progreso Rich; --dry-run = JSON del plan
  verify NAME [--format json|markdown]   # VerifyReport (== POST /verify/{name})
  lock NAME [--update]                   # LockResult (== POST /lock/{name})
  status TASK_ID | plan TASK_ID | result TASK_ID   # JSON == GET /status|/plan|/result
Exit: 0 OK · 1 no encontrado/genérico · 2 validación · 3 tarea FAILED · 4 IO
      · 5 verificación fallida / lock rechazado · 6 UNLOCKED
```
`materialize` termina con el código de su `outcome`: `DOWNLOAD_FAILED` → 3, `MISMATCH`/`RECIPE_CHANGED` → 5,
`UNLOCKED` → 6, `OK`/`NOT_VERIFIED` → 0.

### 2.2 API (FastAPI) — `.../ft_datasets/api.py`, `router = APIRouter(prefix="/datasets")`
| Método | Ruta | Nombre de ruta | Respuesta |
|---|---|---|---|
| GET | `/recipes` | `datasets_recipes` | `list[RecipeSummary]` |
| GET | `/recipes/{name}` | `datasets_recipe` | `{recipe, items, lock}` · 404 |
| POST | `/materialize/{name}` (query: `verify=true`, `refetch=false`) | `datasets_materialize` | `202` `DispatchResponse` · 400 · 404 |
| GET | `/status/{task_id}` | `datasets_status` | `MaterializeProgress` · 404 |
| GET | `/plan/{task_id}` | `datasets_plan` | `MaterializePlan` · 404 |
| GET | `/result/{task_id}` | `datasets_result` | `MaterializeResult` · 404 · 409 |
| POST | `/verify/{name}` | `datasets_verify` | `VerifyReport` (200 con `status` en el cuerpo) · 400 · 404 |
| POST | `/lock/{name}` (query: `update=false`) | `datasets_lock` | `LockResult` · 409 si `REJECTED` |

- Nombres de ruta únicos (lección de T7.1 de `ft_indicators`: `url_for` resuelve por nombre en toda la app compuesta).
- `POST /materialize` crea la tarea y lanza el trabajo con `BackgroundTasks` (subprocesos con
  `asyncio.create_subprocess_exec`); `verify`/`lock` corren en `run_in_threadpool` (Art. 3).
- `task_id` validado con `^[0-9a-f-]{36}$`; `name` con `RecipeName`.

## 3. Algoritmos Clave

### 3.1 Recetas — `finances_core/datasets/recipes.py`
- `load_recipe(recipes_dir, name) -> DatasetRecipe`: lee `name + ".toml"` con `tomllib`; el archivo debe estar dentro
  de `recipes_dir` (resolución + comprobación de prefijo); `name` del TOML == nombre del archivo.
- `list_recipes(recipes_dir)`, `lock_path(recipes_dir, name)`, `load_lock` / `write_lock` (atómico: temporal + `replace`,
  JSON con `indent=2`, claves ordenadas, salto de línea final, para diffs legibles en PR).
- `validate_reproducible(recipe, today, settle_days)` (RF-2).

### 3.2 Hash `ftds-v1` — `finances_core/datasets/hashing.py`
Entrada: `pl.DataFrame` del ítem filtrado a `[start_ms, end_ms)`, validado (dtypes canónicos: `Int64` para
`open_time_ms`/`trades_count`, `Float32` resto; `open_time_ms` estrictamente creciente, sin nulos).
```
col_digest(name, dtype, n, values, nulls) =
    SHA256( b"ftds-v1\0col\0" + name + b"\0" + dtype + b"\0" + u64le(n)
            + SHA256(values_stream) + SHA256(null_positions_stream) )
values_stream:   int64   -> 8 bytes LE por fila (nulo -> 8 bytes 0)
                 float32 -> 4 bytes LE del patrón IEEE (NaN -> 0x7FC00000; nulo -> 4 bytes 0)
null_positions:  u64le(índice de fila) por cada nulo
content_hash   = "sha256:" + SHA256( b"ftds-v1\0item\0" + Σ_{c ∈ strict, en orden} (name + b"\0" + col_digest) )
month_hashes[m]= mismo content_hash restringido a las filas del mes UTC m
```
- Implementación: `series.to_numpy()` (sin copia si no hay nulos) → `.astype("<i8")` / `.view("<u4")` tras forzar
  *little-endian*; `np.where(np.isnan(x), 0x7FC00000, bits)` para NaN. El cómputo es en *streaming* por mes
  (un hasher por columna y otro por mes), sin cargar el rango entero (Art. 19).
- **Por qué es estable entre SO/CPU**: el hash no ejecuta aritmética de coma flotante; solo reinterpreta bits. Los
  valores almacenados provienen de **una única conversión correctamente redondeada** (texto decimal de Binance →
  `Float32`; `float64` de Yahoo → `Float32`), operación exacta por IEEE 754 e idéntica en x86/ARM. La lección "el
  mismo cálculo difiere en el último bit entre CPUs" aplica a cadenas de operaciones (FMA, reducciones SIMD,
  funciones trascendentes), no a una conversión con redondeo al par más cercano. La única variación de bits
  conocida entre CPUs en datos sin aritmética es la **carga útil de NaN** (x86 produce `0xFFC00000`, ARM
  `0x7FC00000`): por eso se canonicaliza. `-0.0` se conserva (es dato).
- **Riesgo residual medido en T6.2**: el descargador de Binance puede convertir texto→`Float32` directamente o vía
  `Float64` (doble redondeo, distinto en casos de empate raros) según la fuente (Public Data CSV vs REST). Si
  ocurriera, el hash dependería de la fuente elegida por el planificador; Q1 2024 es 100 % Public Data, así que la
  receta inicial no se ve afectada, y T6.2 lo mide comparando un mes descargado por ambas vías.
- **Independencia del escritor**: el hash depende solo de los valores; T2.2 lo prueba reescribiendo el mismo
  `DataFrame` con `zstd`/`snappy`/sin compresión, distintos `row_group_size` y con/sin estadísticas.
- **Vectores dorados**: T2.1 fija en el test el hex esperado de un `DataFrame` sintético con `-0.0`, subnormales,
  `±inf`, `float32::MAX`, NaN con cargas distintas y nulos. Corre en la suite por defecto en Windows y Ubuntu.

### 3.3 Hash canónico de la receta
`recipe_sha256 = SHA256(json.dumps([item.key for item in recipe.items()], separators=(",", ":")))` sobre la lista
**ordenada** de claves. No depende de comentarios, espacios, orden de bloques ni de `description`; sí de cualquier
cambio de exchange, `market_type`, símbolo, timeframe o rango.

### 3.4 Lectura del ítem — `finances_core/datasets/store.py`
`scan_item(datasets_dir, item) -> Iterator[(month, pl.DataFrame)]`: recorre solo
`{datasets_dir}/{exchange}/market_type={mt}/symbol={sym}/timeframe={tf}/year={Y}/month={MM}/data.parquet` para los
meses que solapan `[start_ms, end_ms)` (reutiliza `finances_core.marketdata.inventory.partition_base_dir`), filtra el
rango y valida (reutiliza las reglas de `finances_core.indicators_store.source.validate_source`).
`delete_item_partitions(datasets_dir, item)` (solo para `--refetch`, solo bajo `datasets_dir`).

### 3.5 Verificación y clasificación — `finances_core/datasets/verify.py`
`compute_item_lock(datasets_dir, item) -> ItemLock | None` (None = `MISSING`) y
`verify_recipe(recipe, lock, datasets_dir) -> VerifyReport`:
- Compara `content_hash`; si difiere, clasifica: `ROWS_CHANGED` si cambia `row_count`, `first/last_open_time_ms` o
  `column_hashes["open_time_ms"]`; si no, `SCALE_RESTATED` si cambian todas las columnas de precio y
  `close_actual/close_bloqueado` de las 3 sondas coincide con tolerancia relativa 1e-3 (factor = mediana); si no,
  `VALUES_CHANGED`. `months_changed` = meses con hash distinto.
- `informational` distinto → `OK_WITH_WARNINGS`.
- `missing_candles` (binance): `(end_ms − start_ms) / tf_ms − row_count` con `TF_MS_MAP`.
Vive en `finances_core` para que los tests `real_data` de cualquier paquete verifiquen sin importar `ft_datasets`
(Art. 4).

### 3.6 Testing — `finances_core/datasets/testing.py`
`require_dataset(recipe_name, key, settings=None) -> pl.LazyFrame`: carga receta y *lock*, verifica **solo ese ítem**
y devuelve `scan_parquet` del rango; `pytest.fail` con el `ItemVerification` si no es `OK`/`OK_WITH_WARNINGS`
(`pytest` se importa de forma perezosa: `finances-core` no depende de `pytest` en runtime).

### 3.7 Materialización — `ft_datasets/materializer.py` + `runner.py` + `service.py`
1. `VALIDATING`: `load_recipe`, `validate_reproducible`, `load_lock`.
2. `PRECHECKING`: `verify_recipe` → ítems `OK`/`OK_WITH_WARNINGS` se omiten; `MISMATCH` con `refetch` →
   `delete_item_partitions`.
3. Bloques: por cada `[[datasets]]`, los símbolos y timeframes de sus ítems pendientes (producto cartesiano; un
   superconjunto es inocuo porque el descargador es idempotente). Comando:
   ```
   ft_market_data_binance  --log-format json -q download -s S... -t TF... --start <ms> --end <end_ms-1> -m <spot|futures>
   ft_market_data_yfinance --log-format json -q download -s S... -t TF... --start <ms> --end <end_ms-1>
   ```
   (posición exacta de los flags globales según la CLI de cada descargador; T3.1 la fija con un test de `--help`).
4. `DOWNLOADING`: `runner.run_block(argv, env, timeout_s) -> BlockRun`. Ejecutable resuelto en
   `sysconfig.get_path("scripts")` (mismo entorno que `ft_datasets`) y, si no está, en `PATH`.
   Entorno del hijo = `os.environ` + `FT_PROJECT_ROOT`, `FT_PARQUET_DIR=<FT_DATASETS_DIR absoluto>`,
   `FT_TASKS_DB_PATH` (absolutos), para que el hijo escriba exactamente donde el padre lee. Bloques en secuencia.
   `downloader_task_id` = primer `task_id` encontrado en las líneas JSON de `stderr`.
   `Runner` es un `Protocol` inyectable: los tests usan un *runner* falso que escribe Parquet sintético.
5. `VERIFYING`: `verify_recipe` completo (salvo `--no-verify`).
6. Cierre con `TaskRecorder` de `finances_core.marketdata.tasks_lifecycle` (fases monótonas, `datasets_plan` como
   prefijo de plan).

### 3.8 CI — `.github/workflows/real-data.yml`
```yaml
on:
  workflow_dispatch:
  schedule: [{cron: "0 5 * * 1"}]                       # lunes 05:00 UTC
  pull_request: {types: [labeled, synchronize]}
jobs:
  real-data:
    if: github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'real-data')
    runs-on: ubuntu-24.04                                # misma imagen fijada que ci.yml
    timeout-minutes: 60
    permissions: {contents: read, issues: write}         # solo GITHUB_TOKEN (RF-10 bis)
    concurrency: {group: "real-data-${{ github.ref }}", cancel-in-progress: true}
    env: {FT_DATASETS_DIR: "${{ github.workspace }}/.ft-datasets"}
    steps:
      - checkout · setup-python 3.12 (cache pip) · instalación editable core → downloader → analytics[dev] → api
      - ft_setup init
      - id: cache
        uses: actions/cache/restore@v4
        with:
          path: .ft-datasets
          key: ft-datasets-ftds-v1-${{ hashFiles('datasets/*.toml', 'datasets/*.lock.json') }}
          restore-keys: ft-datasets-ftds-v1-
      - id: materialize
        name: Materializar y verificar      # timeout-minutes: 30; guarda el exit code en outputs.exit
        run: ft_datasets --log-format json materialize benchmark-v1 --refetch
      - uses: actions/cache/save@v4
        if: success() && steps.cache.outputs.cache-hit != 'true'
      - id: tests
        name: Tests real_data              # timeout-minutes: 20; resumen de fallos en reports/pytest.txt
        run: pytest -m real_data en finances-core, finances-downloader y finances-analytics
      - if: always(): ft_datasets verify benchmark-v1 --format markdown > reports/verify.md (y >> $GITHUB_STEP_SUMMARY)
                      y ft_datasets verify --format json > reports/benchmark-v1.json (artifact)
      - name: Issue de fallo semanal       # RF-10 bis
        if: failure() && github.event_name == 'schedule'
        env: {GH_TOKEN: "${{ github.token }}"}
        run: scripts/ci/real_data_issue.sh benchmark-v1 "${{ steps.materialize.outputs.exit }}" "${{ steps.tests.outcome }}"
```
- Solo `ubuntu-24.04`, la misma imagen fijada en `ci.yml` para la calibración de benchmarks: ambos workflows corren
  sobre el mismo sistema y el cambio de imagen es una decisión explícita (un PR que toca las dos), nunca un salto
  silencioso de `ubuntu-latest`. La portabilidad del hash entre SO la cubren los vectores dorados en la matriz por defecto, y
  el *lock* inicial se genera en Windows (T6.1) y se verifica en Ubuntu, lo que lo prueba con datos reales.
- La caché solo se guarda tras verificación `OK`: nunca se cachean datos que no coinciden. `restore-keys` reutiliza
  la caché previa si cambia la receta; `--refetch` corrige lo que ya no coincide y la pre-verificación evita
  re-descargar lo que sí coincide.
- Lectura del resultado: exit 3 = fallo de red/proveedor (re-ejecutar); exit 5 = deriva de datos (el resumen
  muestra `drift`, columnas y meses; se corrige con `lock --update` en un PR revisado); fallo en `pytest` = regresión
  de código.

#### 3.8.1 Issue automático — `scripts/ci/real_data_issue.sh` (RF-10 bis)
- **Tipo** (prioridad): exit de materialización 3 → `download`; 5 → `drift`; 6 → `unlocked`; materialización 0 y
  `steps.tests.outcome == failure` → `tests`. Cualquier otro fallo (p. ej. instalación) → `infra`.
- **Etiquetas**: `real-data` y `real-data:<tipo>`; se crean con `gh label create --force` (idempotente).
- **De-duplicación**: `gh issue list --state open --label real-data --label "real-data:<tipo>" --json number --limit 1`;
  si devuelve un número → `gh issue comment <n> --body-file body.md`; si no →
  `gh issue create --title "[real-data] <tipo>: benchmark-v1" --label ... --body-file body.md`.
- **Cuerpo** (`body.md`): enlace a la ejecución (`$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID`),
  fecha, `reports/verify.md` (tabla por dataset con estado, `drift`, `scale_factor`, columnas y meses) y, para
  `tests`, las líneas de resumen de `reports/pytest.txt`. Si `verify.md` no existe (fallo temprano), se indica.
- El script termina con `|| echo "::warning::..."`: un fallo al gestionar el issue no cambia el resultado del job,
  que ya está en rojo por el paso original.
- Las ejecuciones manuales y por etiqueta no ejecutan el paso (`github.event_name == 'schedule'`).

## 4. Estructura de Archivos Objetivo
```
datasets/
├── benchmark-v1.toml
└── benchmark-v1.lock.json                # generado por `ft_datasets lock` (T6.1), revisado en PR
packages/finances-core/src/finances_core/
├── schemas/datasets.py
└── datasets/
    ├── __init__.py
    ├── recipes.py  hashing.py  store.py  verify.py  testing.py
packages/finances-core/tests/unit/test_datasets_*.py
packages/finances-downloader/src/finances_downloader/ft_datasets/
├── __init__.py
├── runner.py  materializer.py  service.py  api.py  cli.py  errors.py
packages/finances-downloader/tests/ft_datasets/
├── conftest.py                           # recetas temporales + FakeRunner (sin red ni subprocesos)
├── test_*.py
└── real_data/test_benchmark_v1.py        # marcador real_data
packages/finances-analytics/tests/real_data/test_indicators_real.py   # marcador real_data
.github/workflows/real-data.yml
scripts/ci/real_data_issue.sh            # issue de-duplicado ante fallo semanal (RF-10 bis)
```

## 5. Dependencias
- `finances-core`: `numpy>=1.26` explícito (bits de `float32`; ya llega vía Polars/pandas, se declara por uso directo).
  `tomllib` es estándar (Python ≥ 3.11). Sin `tomli-w`: el *lock* es JSON y la receta TOML nunca se reescribe.
- `finances-downloader`: script `ft_datasets = "finances_downloader.ft_datasets.cli:app"`; sin dependencias nuevas.
- Marcador `real_data` registrado en `finances-core`, `finances-downloader` y `finances-analytics`;
  `addopts` con `-m 'not real_data'` (y `not benchmark` donde ya existe).
- Antes de implementar: consultar Context7 para Polars (`to_numpy`, `scan_parquet` con filtros), Typer, FastAPI
  (`BackgroundTasks`) y Pydantic (`StringConstraints`, `date` desde TOML).

## 6. Decisiones Arquitectónicas Clave (ADR)
| ID | Decisión | Motivo |
|---|---|---|
| ADR-D01 | **Ubicación**: `ft_datasets` vive en `finances-downloader` e invoca a los descargadores por su **CLI pública como subproceso** (opción b). Receta, hash, verificación y `require_dataset` viven en `finances_core.datasets`. **No requiere RFC** | Art. 4 prohíbe *imports* entre `ft_*` "sin importar el paquete": (d) un paquete nuevo que importe `finances_downloader.ft_market_data_*` sigue siendo una herramienta `ft_*` importando hermanas; (a) `finances-api` solo puede importar *routers* y convertirla en herramienta (CLI propia, lógica de negocio) mezcla la raíz de composición con una utilidad y exigiría enmendar el Art. 4; (c) la API HTTP obliga a tener `ft_serve` levantado (también en CI) para usar la CLI y acopla a un proceso servidor. La CLI es ya un contrato público y versionado de cada descargador (Art. 1, exit codes en sus specs); el Art. 3 contempla subprocesos aislados. Vivir en `finances-downloader` garantiza que los ejecutables están instalados en el mismo entorno. La verificación no necesita a los descargadores: lee Parquet con el núcleo |
| ADR-D02 | Hash `ftds-v1` sobre contenido canónico (bits exactos de `float32`/`int64`, NaN canónico), no bytes de archivo | Los bytes Parquet cambian con compresión, *row groups*, estadísticas y versión del escritor; los valores no. Sin aritmética flotante en el hash: estable entre SO/CPU (§3.2) |
| ADR-D03 | *Lock* separado (`<nombre>.lock.json`) en lugar de hashes dentro del TOML | La receta es editada por personas (comentarios); reescribir TOML los perdería y requeriría `tomli-w`. Convención de *lockfiles* (Cargo, uv): la receta declara, el *lock* fija; ambos versionados y revisados en PR. `recipe_sha256` liga el *lock* a la receta |
| ADR-D04 | Almacén dedicado `FT_DATASETS_DIR` (caché reconstruible), distinto de `FT_PARQUET_DIR` | Las descargas propias del usuario (p. ej. AAPL guardado antes de un *split*) no contaminan la verificación; permite `--refetch` sin tocar datos del usuario; es exactamente el directorio que cachea CI. Es un árbol Hive normal: `ft_data_viewer` lo lee con `FT_PARQUET_DIR` apuntando a él |
| ADR-D05 | Columnas estrictas vs informativas (`adj_close` informativa) | Yahoo re-expresa `adj_close` con cada dividendo nuevo: en estricto, AAPL fallaría cada trimestre sin que cambie nada útil para los tests |
| ADR-D06 | Solo rangos cerrados con margen de asentamiento y solo `1d/1w/1M` en Yahoo | Velas en formación, revisiones tardías y el historial intradía de Yahoo (30–730 días) hacen imposible reproducir lo demás |
| ADR-D07 | El *lock* solo se escribe con `lock` explícito; actualizarlo exige `--update` | Un hash que se actualiza solo no verifica nada; la deriva del proveedor debe pasar por revisión humana |
| ADR-D08 | `materialize` es tarea con *polling*; `verify`/`lock` son síncronos en *threadpool* | La descarga es larga (Art. 18); la verificación es local y acotada (RF-5: ≤ 5 s para `benchmark-v1`). Si las recetas crecen, `verify` puede pasar a tarea sin romper el contrato de CLI |
| ADR-D09 | Pre-verificación y omisión de ítems `OK` | El descargador de Yahoo re-pide tramos de fin de semana en cambios de mes (limitación conocida RF-6 de `ft_market_data_yfinance`); sin omisión, CI con caché caliente seguiría usando la red |
| ADR-D10 | Recetas inmutables por nombre (`-v1`, `-v2`); `RECIPE_CHANGED` si se editan tras bloquear | Una receta con nombre fijo significa siempre los mismos datos; evolucionar = receta nueva |
| ADR-D11 | Bloques en secuencia | Cada descargador ya paraleliza internamente con su propio rate-limit (Art. 14); paralelizar bloques solo suma riesgo de 429 |
| ADR-D12 | Suite `real_data` opt-in, workflow separado solo en Ubuntu, caché guardada solo tras verificación `OK` | La suite por defecto sigue sin red y determinista; CI descarga una vez por versión de receta y nunca cachea datos inválidos |
| ADR-D14 | Fallo semanal → issue automático de-duplicado por tipo (etiquetas `real-data:<tipo>`), solo con `GITHUB_TOKEN` | Un fallo programado sin dueño se pierde en los correos; un issue por tipo evita ruido semanal repetido y separa red, deriva y regresión de código |
| ADR-D15 | Imagen `ubuntu-24.04` fijada, igual que `ci.yml` | Coherencia con la calibración de benchmarks; cambiar de imagen debe ser explícito |
| ADR-D13 | Clasificación de deriva con 3 sondas de `close` y hashes por columna y por mes | Sin guardar los datos esperados, distingue *split* re-expresado (cociente constante), filas cambiadas y correcciones puntuales, y localiza el mes |

## 7. Integración con `finances-core` y la suite
- `finances_api.create_app()` monta `finances_downloader.ft_datasets.api.router` (`/datasets`); spec/plan/tasks de
  `finances_api` actualizados (RF-1). `/datasets` no colisiona con `/data/datasets` de `ft_data_viewer`.
- `ft_setup`: `FT_DATASETS_RECIPES_DIR`, `FT_DATASETS_DIR`, `FT_DATASETS_DOWNLOAD_TIMEOUT_S`, `FT_DATASETS_SETTLE_DAYS`
  en `env_list.conf` y `templates/env_list.conf.j2` (idénticos) con `source_tool="ft_datasets"`; `init` crea
  `DATASETS_DIR`. `datasets/` (recetas) se versiona; `data/datasets/` ya está ignorado por `data/` en `.gitignore`.
- README: §2.2 (utilidad), §3 (ejemplos `list`/`materialize`/`verify`/`lock`), servicio (`/datasets`), §4 (cómo
  correr `pytest -m real_data` y el workflow).
- Art. 4: test AST — `ft_datasets` no importa `finances_downloader.ft_*`, `finances_analytics`, `finances_api`; la
  única referencia a los descargadores son los nombres de sus *console scripts*.
- Constitución: sin cambios (ADR-D01). Art. 20 — Resiliencia: delegada en los descargadores (rate-limit y reintentos)
  más *timeout* por bloque; Desempeño: RF-5 medido en el smoke (sin benchmark en CI, justificado: el coste dominante
  es la red).
