# Plan — `ft_market_data_manager` (MVP v1.0)

## 0. Decisiones de Diseño y Prerrequisitos

- **Reutilización vía `finances-core` (Art. 4)**: el manager no importa de `ft_data_viewer`, `ft_market_data_binance` ni `ft_market_data_yfinance`. Usa lo que ya vive en `finances_core`: `marketdata.hive_writer` (`atomic_write`, `partition_dir`, `merge_partition`, `HIVE_KEYS`, `DEFAULT_FILE_NAME`), `marketdata.months`, `marketdata.tasks_lifecycle` (`TaskRecorder`, `load_task_row`, `ensure_finished`), `tasks.SQLTaskManager` y `export`.
- **Prerrequisito de refactor (T1.1)**: el descubrimiento de datasets Hive (`_discover`, `dataset_files`, `resolve_dataset_path` en `ft_data_viewer/scanner.py`) se extrae a `finances_core/marketdata/dataset_layout.py`; `ft_data_viewer` pasa a importarlo desde ahí, sin cambio de comportamiento (mismo procedimiento que `export.py`). Así viewer y manager comparten un único parser del layout.
- **Un único código de escritura**: toda partición se escribe con `atomic_write`; el manager no introduce otro writer.
- **Sin SQLite**: descartado por el usuario (Parquet, CSV y JSON Lines bastan). Sin `float64` ni RFC nuevo.
- **Consolidación y lectura**: `ft_data_viewer export` ya consolida un rango en un archivo; el manager no lo duplica ni expone lecturas por rango.
- **Sin cambios en los descargadores**: la concurrencia con ellos se cubre con verificación optimista por partición (ADR-M03).

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquemas (`packages/finances-core/src/finances_core/schemas/market_data_manager.py`)

```python
class DatasetSelector(BaseModel):
    """Misma identificación que ft_data_viewer; las listas se combinan con AND entre campos."""
    exchange: list[str] = []
    market_type: list[str] = []
    symbol: list[str] = []
    timeframe: list[str] = []
    start_ms: int | None = None      # acota meses (inclusive)
    end_ms: int | None = None

class PartitionResult(BaseModel):
    dataset: str                       # "binance/spot/BTCUSDT/1h"
    year: int
    month: int
    result: Literal["changed", "unchanged", "conflict", "error", "planned"]
    files_before: int
    files_after: int
    rows_before: int
    rows_after: int
    bytes_before: int
    bytes_after: int
    detail: str | None = None          # error o ejemplo de duplicado

class ManagerRequest(DatasetSelector):
    dry_run: bool = False

class PruneRequest(ManagerRequest):
    before_ms: int | None = None
    all: bool = False
    confirm: bool = False              # API; la CLI usa --force / prompt
    # validador: exactamente uno de before_ms | all; all exige algún filtro de dataset

class ImportRequest(BaseModel):
    source: Path                        # fuera de FT_PARQUET_DIR
    format: Literal["parquet", "csv", "jsonl"] | None = None   # None => por extensión
    exchange: str                       # destino explícito, nunca deducido del archivo
    market_type: str
    symbol: str
    timeframe: str
    create: bool = False                # permite crear el dataset destino
    ts_column: str = "open_time_ms"
    ts_unit: Literal["s", "ms", "us", "ns"] = "ms"
    on_conflict: Literal["skip", "fail", "replace"] = "skip"
    skip_invalid: bool = False
    dry_run: bool = False
    confirm: bool = False               # API; la CLI usa --force / prompt
    force: bool = False                 # permite replace con divergencia > 1 %


class ImportPlan(BaseModel):
    destination: str                    # "binance/spot/BTCUSDT/1h"
    rows_in_file: int
    rows_new: int
    rows_conflict: int
    rows_rejected: int
    range_ms: tuple[int, int]
    partitions_new: list[str]
    partitions_modified: list[str]
    max_close_divergence: float | None  # solo con conflictos


class ManagerTaskResponse(BaseModel):   # 202
    task_id: str
    poll_url: str

class ManagerResult(BaseModel):
    action: Literal["compact", "dedupe", "prune", "import"]
    dry_run: bool
    partitions: list[PartitionResult]
    import_plan: ImportPlan | None = None   # solo import
```

### 1.2 Configuración

Sin variables nuevas: usa `FT_PARQUET_DIR` y `FT_TASKS_DB_PATH` (RF-9). No hay cambios en `env_list.conf`.

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `ft_market_data_manager`

| Comando | Opciones principales |
|---------|----------------------|
| `compact` | selector (`--exchange/--market-type/--symbol/--timeframe` repetibles, `--start-ms`, `--end-ms`), `--dry-run`, `--format table\|json` |
| `dedupe` | ídem `compact` |
| `prune` | selector, `--before-ms T` \| `--all`, `--dry-run`, `--force` (omite la confirmación), `--format` |
| `import` | `--source PATH`, `--format parquet\|csv\|jsonl`, destino obligatorio (`--exchange`, `--market-type`, `--symbol`, `--timeframe`), `--create`, `--ts-column`, `--ts-unit`, `--on-conflict skip\|fail\|replace`, `--skip-invalid`, `--dry-run`, `--force` |

Flags globales como en el resto de la suite: `--verbose`, `--quiet`, `--log-format`. Códigos de salida: spec RF-10. Entry point `ft_market_data_manager = "finances_downloader.ft_market_data_manager.cli:app"`.

### 2.2 API (FastAPI) — prefijo `/market-data/manager`

| Método | Ruta | Body | Respuesta |
|--------|------|------|-----------|
| POST | `/compact` | `ManagerRequest` | 202 `ManagerTaskResponse` |
| POST | `/dedupe` | `ManagerRequest` | 202 |
| POST | `/prune` | `PruneRequest` | 202 (400 sin `confirm` y sin `dry_run`) |
| POST | `/import` | `ImportRequest` | 202 (400 sin `confirm` y sin `dry_run`) |
| GET | `/status/{task_id}` | — | estado y contadores (RF-7) |
| GET | `/result/{task_id}` | — | `ManagerResult` (409 si no terminó, como binance/yfinance) |

Cada POST valida de forma síncrona (selector, filtros de `prune --all`, existencia y ubicación de `source`, destino), crea la tarea con `SQLTaskManager` y despacha `BackgroundTasks` con `run_in_threadpool` para el trabajo Polars/Parquet (Art. 3). Se monta desde `finances_api.create_app()` como los demás routers (la dependencia va solo en ese sentido, AGENTS.md §1).

## 3. Algoritmos Clave

### 3.1 Recorrido y verificación optimista (común)

```
FUNCTION plan(selector) -> list[Partition]:
    datasets = dataset_layout.discover(FT_PARQUET_DIR) FILTERED BY selector
    partitions = months of each dataset within [start_ms, end_ms]
    snapshot[p] = [(file, size, mtime_ns) for file in p.files sorted]
FUNCTION commit(p, new_df | delete):
    IF current_snapshot(p) != snapshot[p]: RETURN "conflict"       # RF-6
    IF new_df: atomic_write(new_df, p.dir / "data.parquet"); remove sobrantes   # tras el reemplazo
    ELSE:      remove p.files; remove empty hive dirs
    RETURN "changed"
```

### 3.2 `compact` y `dedupe`

```
read = pl.concat([pl.scan_parquet(f) for f in sorted(p.files)]) -> eager (una partición mensual)
compact: out = read.sort("open_time_ms", maintain_order=True)             # sin perder filas
dedupe:  out = read.unique(subset="open_time_ms", keep="last", maintain_order=True).sort("open_time_ms")
unchanged IF (len(p.files)==1 AND out.equals(read)); en otro caso commit(p, out)
```

El esquema Arrow se conserva (`float32`/`int64`); si una partición no cumple el esquema canónico se informa `error` y no se toca (el diagnóstico de esquema es del viewer).

### 3.3 `prune`

```
cutoff = before_ms
candidatas = [p for p in plan(selector) IF month_end(p) <= cutoff]   # --all: todas
IF dry_run: RETURN planned
IF NOT (force OR confirm): abort                                      # RF-3
FOR p IN candidatas: commit(p, delete)
```

### 3.4 `import`

```
FASE 1 — validar y planear (no escribe nada)
  df = leer(source, format)  # lazy; CSV/JSONL con esquema explícito, nunca inferido
  df = renombrar ts_column -> open_time_ms; convertir ts_unit -> ms (un único punto)
  exigir columnas == esquema canónico del exchange destino (sin symbol/timeframe/market_type)
  castear a float32/int64 con range-check (precio > 0, volumen >= 0); clasificar filas inválidas
  exigir plausibilidad: 2000-01-01 <= open_time_ms <= now + 1d
  exigir alineación: is_aligned(open_time_ms, timeframe)   # grilla del timeframe destino
  exigir open_time_ms únicos en el archivo
  dataset = resolver destino (404 si no existe y NOT create)
  por partición destino (año, mes): existing = leer si existe
      conflictos = df ∩ existing por open_time_ms; divergencia = max |close_a - close_b| / close_b
  plan = ImportPlan(...)
  SI hay inválidas y NOT skip_invalid, o fallo de plausibilidad/alineación/duplicados: ValidationError (sin escribir)
  SI on_conflict == fail y conflictos > 0: ConflictError
  SI on_conflict == replace y divergencia > 0.01 y NOT force: ConflictError
FASE 2 — escribir (tras dry_run/confirmación)
  por partición: merged = existing ∪ nuevas  (skip: existentes prevalecen; replace: reemplaza solo los valores de ese open_time_ms)
  commit(p, merged)   # verificación optimista + atomic_write
```

`merge_partition` de `hive_writer` hace prevalecer lo nuevo (contrato de los descargadores); aquí el valor por defecto es el contrario (`skip`), por eso el manager implementa su propia combinación en `planner.py` y reutiliza solo `atomic_write`/`partition_dir`.

## 4. Estructura de Archivos Objetivo

```
packages/finances-core/src/finances_core/
├── marketdata/dataset_layout.py        # extraído de ft_data_viewer/scanner.py (T1.1)
└── schemas/market_data_manager.py      # §1.1

packages/finances-downloader/src/finances_downloader/ft_market_data_manager/
├── __init__.py
├── cli.py              # Typer app (4 comandos)
├── api.py              # FastAPI router (7 endpoints)
├── service.py          # run_compact / run_dedupe / run_prune / run_import
├── planner.py          # plan(), snapshots, commit() optimista
├── importer.py         # validación de origen, timestamp y alineación (Fase 1 de import)
├── errors.py           # tipados → exit codes / HTTP
└── models.py           # re-exports desde finances_core

packages/finances-downloader/tests/ft_market_data_manager/
```

## 5. Dependencias

Sin dependencias nuevas: `polars`, `pyarrow`, `typer`, `rich`, `structlog` y `fastapi` ya están en `finances-downloader`.

## 6. Decisiones Arquitectónicas Clave (ADR)

| ADR | Decisión | Justificación |
|-----|----------|---------------|
| M01 | El manager es la única herramienta que modifica el almacén de velas; el viewer permanece de solo lectura | Separa diagnóstico de reparación y evita el `ft_data_cleaner` mencionado en la spec del viewer |
| M02 | Borrado y reescritura **por partición mensual** | Es la unidad de escritura atómica de los descargadores; evita recortes parciales |
| M03 | Verificación optimista (tamaño + `mtime_ns`) en lugar de un lock compartido | Los descargadores no se modifican; un cambio concurrente deja la partición en `conflict` y nunca se pierde una escritura ajena |
| M04 | `import` valida y planea antes de escribir; el destino es siempre explícito, el timestamp nunca se adivina y las filas existentes nunca cambian de `open_time_ms` | Una importación mal alineada (zona horaria, unidad, timeframe) no debe corromper un dataset |
| M05 | `dedupe` conserva la última fila en orden de lectura | Regla determinista; el almacén no guarda criterio de frescura por fila |
| M06 | Rellenar gaps queda fuera: requeriría invocar a otra `ft_*` (Art. 4) | Se mantiene el aislamiento; el viewer detecta y el usuario repara con el descargador |
| M07 | Todas las operaciones que modifican van por tarea en la API y por ejecución síncrona en CLI, con la misma `service.py` | Dualidad (Art. 1) y despacho (Art. 18) |

## 7. Integración

- `finances_api`: montar `ft_market_data_manager.api.router` y reflejarlo en `specs/finances_api` (spec/plan/tasks) y en `GET /health` solo si se requiere un chequeo (no se prevé: no hay base propia).
- `ft_setup`: sin cambios (no hay variables nuevas).
- `ft_data_viewer`: pasa a importar el layout desde `finances_core.marketdata.dataset_layout` (T1.1); se actualiza la fila "Modificación de datos" de su spec para apuntar a este manager.
- README: tabla de paquetes y mapa 2.2.
- Lectura de datos para aplicaciones cliente (`GET` por rango): resuelta en `ft_data_viewer` (RF-10, `GET /data/datasets/{...}/candles`); el manager no expone lecturas.
