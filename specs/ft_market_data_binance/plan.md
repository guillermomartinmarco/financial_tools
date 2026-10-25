# Plan — `ft_market_data_binance` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquemas de Entrada (`packages/finances-core/schemas/market_data.py`)

```python
from enum import Enum
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal, List
from datetime import datetime

class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"

class Timeframe(str, Enum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    M1_MONTH = "1M"

class DownloadRequest(BaseModel):
    symbols: List[str] = Field(min_length=1, max_length=100)
    timeframes: List[Timeframe] = Field(min_length=1)
    start_ms: int = Field(gt=0)  # int64 ms UTC
    end_ms: Optional[int] = Field(default=None, gt=0)
    market_type: MarketType = MarketType.SPOT
    limit_per_request: int = Field(default=1000, ge=1, le=1000)
    max_concurrent: Optional[int] = Field(default=None, ge=1, le=50)

    @field_validator("end_ms")
    @classmethod
    def validate_time_range(cls, v, info):
        if v is not None and info.data.get("start_ms") and v <= info.data["start_ms"]:
            raise ValueError("end_ms must be greater than start_ms")
        return v

class DownloadPlanItem(BaseModel):
    source: Literal["public_data", "rest_api"]
    symbol: str
    timeframe: str
    market_type: str
    # Public Data fields
    url: Optional[str] = None
    expected_checksum: Optional[str] = None
    # REST API fields
    endpoint: Optional[str] = None
    params: Optional[dict] = None
    covers_range_ms: List[int] = Field(min_length=2, max_length=2)

class DownloadPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: List[DownloadPlanItem]

class DownloadProgress(BaseModel):
    task_id: str
    status: Literal["PENDING", "PLANNING", "DOWNLOADING_PUBLIC", "DOWNLOADING_REST", "NORMALIZING", "WRITING_PARQUET", "COMPLETED", "FAILED"]
    phase: str
    symbols_total: int
    symbols_done: int
    timeframes_total: int
    timeframes_done: int
    downloaded_bytes: int = 0
    processed_candles: int = 0
    gaps_found: int = 0
    gaps_filled_public: int = 0
    gaps_filled_rest: int = 0
    errors: List[str] = []
    current_symbol: Optional[str] = None
    current_timeframe: Optional[str] = None

class DownloadResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    parquet_paths: List[str]
    stats: dict  # per symbol/timeframe: candles, bytes, source breakdown
    errors: List[str] = []  # errores por item (RF-9)
    gaps_unfillable: List[dict] = []
```

### 1.2 Esquema Canónico OHLCV (Polars) — `packages/finances-core/schemas/market_data.py`

```python
import polars as pl

OHLCV_SCHEMA = {
    "symbol": pl.Utf8,
    "timeframe": pl.Utf8,
    "market_type": pl.Utf8,
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "volume": pl.Float32,
    "quote_volume": pl.Float32,
    "trades_count": pl.Int64,
    "taker_buy_base_volume": pl.Float32,
    "taker_buy_quote_volume": pl.Float32,
}

OHLCV_PARTITION_COLS = ["symbol", "timeframe", "market_type", "year", "month"]
```

### 1.3 Configuración (`packages/finances-core/src/finances_core/config.py` — extensiones)

```python
# Añadir a FinancesSettings:
FT_BINANCE_PUBLIC_DATA_BASE_URL_SPOT: str = "https://data.binance.vision/data/spot"
FT_BINANCE_PUBLIC_DATA_BASE_URL_FUTURES: str = "https://data.binance.vision/data/futures/um"
FT_BINANCE_PUBLIC_DATA_RATE_CAPACITY: int = 50
FT_BINANCE_PUBLIC_DATA_RATE_REFILL_RATE: float = 10.0

FT_BINANCE_REST_BASE_URL_SPOT: str = "https://api.binance.com"
FT_BINANCE_REST_BASE_URL_FUTURES: str = "https://fapi.binance.com"
FT_BINANCE_REST_RATE_CAPACITY: int = 1200
FT_BINANCE_REST_RATE_REFILL_RATE: float = 20.0
FT_BINANCE_MAX_RETRIES: int = 3
FT_BINANCE_BASE_BACKOFF_MS: int = 1000

FT_DOWNLOAD_MAX_CONCURRENT: int = 5

FT_PARQUET_DIR: pathlib.Path = pathlib.Path("./data/parquet")
FT_SHARE_DIR: pathlib.Path = pathlib.Path("./share")
```

---

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-downloader/src/finances_downloader/ft_market_data_binance/cli.py`

Adaptador delgado sobre `service.py` (Art. 1). `status`/`plan`/`result` imprimen en *stdout*
el mismo JSON que devuelve la API; logs y barras Rich van a *stderr* (Art. 9.2).

```python
app = typer.Typer(name="ft_market_data_binance", no_args_is_help=True, rich_markup_mode="rich")

@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: str = typer.Option("rich", "--log-format"),   # rich | json (otro valor -> exit 2)
) -> None:
    """configure_logging("cli", level_from_flags(v, q), log_format, component="ft_market_data_binance")."""

@app.command()
def download(
    ctx: typer.Context,
    symbols: List[str] = typer.Option(..., "--symbol", "-s"),
    timeframes: List[str] = typer.Option(..., "--timeframe", "-t"),
    start: str = typer.Option(..., "--start"),             # finances_core.timeutils.parse_time_input
    end: Optional[str] = typer.Option(None, "--end"),
    market_type: str = typer.Option("spot", "--market", "-m"),
    max_concurrent: Optional[int] = typer.Option(None, "--max-concurrent", "-c"),
    limit: int = typer.Option(1000, "--limit", "-l"),
    dry_run: bool = typer.Option(False, "--dry-run"),       # imprime el DownloadPlan JSON y sale
    verbose: bool = typer.Option(False, "--verbose", "-v"), # sobrescriben los flags globales
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: Optional[str] = typer.Option(None, "--log-format"),
) -> None: ...

@app.command()
def plan(task_id: str) -> None:    # DownloadPlan JSON (== GET /plan); no existe -> exit 1
@app.command()
def status(task_id: str) -> None:  # DownloadProgress JSON (== GET /status); no existe -> exit 1
@app.command()
def result(task_id: str) -> None:  # DownloadResult JSON (== GET /result); no existe / no terminada -> exit 1
```

- `--force` **no se implementa**: el spec lo declara "RFC futuro" (Casos Límite / No-Go).
- Salida YAML eliminada (no figura en el spec y requeriría `pyyaml`): `plan` sólo emite JSON.
- Exit codes: `0` OK, `1` genérico (incl. tarea/plan no encontrado), `2` validación
  (`ValueError`/`ValidationError`, p. ej. "Cannot download future data"), `3` tarea `FAILED`,
  `4` `OSError` (IO/permisos).

### 2.2 API (FastAPI) — `packages/finances-downloader/src/finances_downloader/ft_market_data_binance/api.py`

Sólo se exporta `router`; la app y el comando `serve` viven en el paquete `finances-api`.

```python
router = APIRouter(prefix="/market-data/binance", tags=["market-data-binance"])

class DispatchResponse(BaseModel):
    task_id: str; status: Literal["ACCEPTED"]; poll_url: str; plan_url: str; result_url: str  # URLs absolutas

@router.post("/download", status_code=202, response_model=DispatchResponse)
async def download_endpoint(request: Request, background_tasks: BackgroundTasks,
                            symbols: List[str] = Query(...), timeframes: List[str] = Query(...),
                            start_ms: int = Query(..., gt=0), end_ms: Optional[int] = Query(None, gt=0),
                            market_type: str = Query("spot", pattern="^(spot|futures)$"),
                            max_concurrent: Optional[int] = Query(None, ge=1, le=50),
                            limit_per_request: int = Query(1000, ge=1, le=1000)) -> DispatchResponse:
    """Valida sin red (400 si inválido) -> create_download_task (PENDING, en threadpool)
    -> background_tasks.add_task(run_download_sync) -> 202 inmediato.
    La planificación (exchangeInfo, inventario, HEAD Public Data) ocurre en background."""

@router.get("/status/{task_id}")  -> DownloadProgress   # 404 si no existe
@router.get("/plan/{task_id}")    -> DownloadPlan       # 404 si no existe
@router.get("/result/{task_id}")  -> DownloadResult     # 404 si no existe, 409 si no terminada
```

Las lecturas SQLite/disco de los `GET` se ejecutan con `run_in_threadpool` (Art. 3).
`DownloadResult.stats` contiene una entrada por `"SYMBOL/tf/market"` más la clave `"errors"`
(lista de errores de la tarea), ya que el contrato `DownloadResult` no tiene campo `errors`.

---

## 3. Algoritmos Clave

### 3.1 `parse_time_input(s: str) -> int` — Parse flexible a ms UTC
Implementado en `finances_core.timeutils` (compartido por todas las herramientas, Art. 4);
`planner.py` ya no lo define.
```python
# Acepta: "2024-01-01", "2024-01-01T00:00:00Z", "1704067200000", "now"
# Retorna: int64 ms UTC
```

### 3.2 `inventory_local_parquet(parquet_dir: Path, symbol: str, tf: str, market: str) -> List[Tuple[int, int]]`
```python
# Escanea {parquet_dir}/binance/market_type={m}/symbol={S}/timeframe={tf}/year=*/month=*/*.parquet
# Retorna lista de rangos (min_open_time_ms, max_open_time_ms) por partición año/mes
# Una sola consulta lazy: scan_parquet(files, include_file_paths=...).group_by(file).agg(min, max)
# (proyecta sólo open_time_ms). Archivos con footer ilegible se omiten con warning.
# El planificador considera cubierta cada partición hasta max + 1 vela, para que el
# borde entre meses consecutivos no genere un gap espurio.
```

### 3.3 `compute_gaps(requested_start: int, requested_end: int, local_ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]`
```python
# Algoritmo: merge intervals + complemento
# 1. Ordenar local_ranges por start
# 2. Merge overlapping/adjacent
# 3. Recorrer desde requested_start hasta requested_end, emitir gaps
```

### 3.4 `check_public_data_availability(gaps, symbol, tf, market, client) -> Tuple[List[DownloadPlanItem], List[Tuple[int, int]]]`
```python
# Para cada gap:
#   - Determinar meses cubiertos (gap_start_ms -> gap_end_ms)
#   - Para cada mes: construir URL Public Data mensual
#     spot:   https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/{TF}/{SYMBOL}-{TF}-{YYYY-MM}.zip
#     futures: https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/{TF}/{SYMBOL}-{TF}-{YYYY-MM}.zip
#   - HEAD request para verificar existencia; si existe, GET <url>.CHECKSUM
#     ("<hex>  <archivo>") -> expected_checksum = "sha256:<hex>" (None si no se publica)
#   - Agregar items source="public_data" para archivos existentes
#   - CADA mes sin archivo (p. ej. el mes en curso) -> sub-rango residual para REST;
#     sub-rangos contiguos se fusionan
```

### 3.5 `plan_rest_fallback(uncovered_gaps, symbol, tf, market, limit, client) -> List[DownloadPlanItem]`
```python
# Para cada gap no cubierto:
#   - Paginar con limit=1000 (máx Binance)
#   - startTime=gap_start, endTime=min(gap_start + limit*tf_ms, gap_end)
#   - Generar items source="rest_api" con endpoint y params
```

### 3.6 `normalize_binance_klines(df, source, *, symbol, timeframe, market_type, start_ms, end_ms) -> pl.DataFrame`
Los parámetros keyword-only aportan las columnas constantes y el rango a filtrar.
Lectores: `read_public_data_zip(zip_path, ...)` (lee el CSV dentro del .zip sin extraer;
detecta cabecera: Futures la trae, Spot no) y `normalize_rest_json(rows, ...)`.
```python
# Public Data CSV columns: open_time, open, high, low, close, volume, close_time, quote_asset_volume, number_of_trades, taker_buy_base_asset_volume, taker_buy_quote_asset_volume, ignore
# REST JSON array: [open_time, open, high, low, close, volume, close_time, quote_asset_volume, number_of_trades, taker_buy_base_asset_volume, taker_buy_quote_asset_volume, ignore]

# Transformaciones:
# 1. Renombrar columnas a esquema canónico
# 2. Cast: open_time -> open_time_ms (Int64), precios/volúmenes -> Float32, trades -> Int64
#    Timestamps >= 1e14 (microsegundos, Spot Public Data desde 2025) se dividen entre 1000
# 3. Agregar columnas: symbol, timeframe, market_type, year, month (derivadas de open_time_ms)
# 4. Filtrar: open_time_ms dentro del rango solicitado (por si CSV trae extra)
# 5. Ordenar por open_time_ms, unique (dedup por open_time_ms, gana la última)
# 6. Validar schema Arrow vs OHLCV_SCHEMA
```

### 3.7 `write_parquet_partitioned(df: pl.DataFrame, parquet_dir: Path) -> List[str]`
```python
# Para cada grupo (market_type, symbol, timeframe, year, month):
#   path = parquet_dir / "binance" / f"market_type={m}" / f"symbol={s}" / f"timeframe={tf}"
#          / f"year={y}" / f"month={mm:02d}" / "data.parquet"
#   SI path existe: df = concat([read_parquet(path), df]).unique("open_time_ms", keep="last")
#   df.sort("open_time_ms").write_parquet(tmp, compression="zstd", statistics=True)
#   (las claves Hive no se guardan como columnas: se infieren de la ruta)
#   os.replace(tmp, path)   # reemplazo atómico
# Retorna lista de rutas de archivos escritos
```

### 3.8 Orquestador Principal — `service.py`
```python
def create_download_task(task_id, request, *, settings=None) -> None
    # INSERT tarea PENDING (en el despacho, antes de planificar) -> GET /status nunca 404

async def run_download(request, task_id, *, settings=None, transport=None, on_phase=None) -> DownloadResult
    # PLANNING: build_download_plan (pasos 1-6). Error de red/validación -> FAILED + error.
    # Símbolos ausentes de exchangeInfo -> se omiten y se registran en errors_json (RF-9).

async def execute_download_plan(plan, max_concurrent=None, *, settings=None, transport=None,
                                on_phase=None) -> DownloadResult
    1. DOWNLOADING_PUBLIC: items public_data con asyncio.Semaphore(max_concurrent);
       checksum mismatch -> 1 reintento -> fallback REST; 404/zip/CSV corrupto -> fallback REST
    2. DOWNLOADING_REST: items rest_api + fallbacks; respuesta vacía -> gaps_unfillable (warning)
    3. NORMALIZING: concat por (symbol, tf, market), dedup, sort (en hilo)
    4. WRITING_PARQUET: write_parquet_partitioned por par (en hilo)
    5. COMPLETED, o FAILED si algún item agotó reintentos de red/HTTP (lo escrito persiste)

def run_download_sync(request, task_id, settings=None) -> None   # entrada BackgroundTasks
def get_progress(task_id, *, settings=None) -> DownloadProgress | None
def get_result(task_id, *, settings=None) -> DownloadResult | None   # TaskNotFinishedError
def resolve_max_concurrent(override, settings) -> int                 # RF-2
```
Las fases sólo avanzan (`PENDING → PLANNING → DOWNLOADING_PUBLIC → DOWNLOADING_REST →
NORMALIZING → WRITING_PARQUET → COMPLETED/FAILED`); todas las escrituras SQLite, lecturas
Polars y zip se ejecutan con `asyncio.to_thread` (Art. 3). El progreso por par
(`pairs_total`/`pairs_done`), la petición y `gaps_unfillable` se guardan en
`result_summary_json`.

### 3.9 Reintentos (`client.py`)
`tenacity.AsyncRetrying`: 1 intento + `FT_BINANCE_MAX_RETRIES` reintentos, espera
`wait_exponential_jitter(multiplier=FT_BINANCE_BASE_BACKOFF_MS/1000, jitter=base, max=60s)`,
sólo ante `418`/`429`/`5xx` o `httpx.TransportError` (nunca otros `4xx`); cada reintento
emite el evento structlog `binance_http_retry`. Aplica a REST y a Public Data (HEAD,
.CHECKSUM y descarga en streaming).

---

## 4. Estructura de Archivos Objetivo

```
packages/finances-downloader/
├── pyproject.toml
├── src/
│   └── finances_downloader/
│       ├── __init__.py
│       └── ft_market_data_binance/
│           ├── __init__.py
│           ├── cli.py              # Typer app
│           ├── api.py              # FastAPI router
│           ├── service.py          # Orquestador principal
│           ├── client.py           # Cliente HTTP dual (Public Data + REST), reintentos, checksums
│           ├── rate_limiter.py     # TokenBucket / DualTokenBucket
│           ├── planner.py          # Lógica pasos 1-6 (inventory, gaps, plan)
│           ├── normalizer.py       # normalize_binance_klines
│           ├── writer.py           # write_parquet_partitioned
│           ├── inventory.py        # inventory_local_parquet, compute_gaps
│           ├── public_data.py      # check_public_data_availability
│           ├── rest_api.py         # plan_rest_fallback, REST client
│           └── models.py           # Re-export de los contratos de finances_core.schemas.market_data
│   (sin logging.py propio: se usa finances_core.logging)
(la base de tareas NO vive en este paquete: es la única `<raíz del proyecto>/share/tasks.db`, Art. 2)
```

---

## 5. Dependencias (pyproject.toml - finances-downloader)

```toml
[project]
name = "finances-downloader"
version = "0.1.0"
dependencies = [
    "finances-core @ file://../finances-core",  # editable local
    "pydantic>=2.8",
    "pydantic-settings>=2.3",
    "typer[all]>=0.12",
    "fastapi>=0.111",
    "httpx>=0.27",
    "polars>=1.6",
    "structlog>=24.1",
    "rich>=13.7",
    "uuid6>=1.11",
    "python-dotenv>=1.0",
    "tenacity>=8.2",           # retry logic
    "zstandard>=0.22",         # compresión Parquet
]

[project.optional-dependencies]
dev = ["pytest>=8.2", "pytest-asyncio>=0.23", "httpx>=0.27", "ruff>=0.5", "mypy>=1.10"]
```

---

## 6. Decisiones Arquitectónicas Clave (ADR)

| ADR | Decisión | Justificación |
|-----|----------|---------------|
| ADR-B01 | Pipeline determinista de 7 pasos (validar → inventario → gaps → public_data → rest_plan → plan_file → execute) | Reproducibilidad, debuggability, resumibilidad |
| ADR-B02 | Public Data prioritario, REST fallback | Public Data = sin rate limit estricto, archivos mensuales completos, checksums |
| ADR-B03 | Plan de descarga persistido en JSON temporal | Inspección manual, debug, re-ejecución parcial, auditoría |
| ADR-B04 | Semáforo `asyncio.Semaphore` para concurrencia (env var + CLI + API) | Control simple, predecible, sin deps externas |
| ADR-B05 | Token Bucket dual (generoso Public Data, estricto REST) | Respeta límites reales de cada fuente |
| ADR-B06 | Normalización inmediata a esquema canónico float32/int64 (Art. 5) | Garantiza contrato downstream; evita conversiones tardías |
| ADR-B07 | Particionado Hive por symbol/timeframe/market_type/year/month | Lecturas eficientes en backtesting (partition pruning) |
| ADR-B08 | Reescritura atómica de la partición (año/mes) combinando filas existentes + nuevas (dedup por `open_time_ms`, gana la nueva) | Idempotencia sin pérdida (RF-4); evita duplicados y no descarta filas fuera del rango descargado |
| ADR-B09 | `finances-downloader` package separado de `finances-core` | Ciclo SemVer independiente; aislamiento de deps pesadas (httpx, polars) |
| ADR-B10 | Auto-registro de variables `FT_*` no declaradas (Art. 16 Constitución) | `FT_DOWNLOAD_MAX_CONCURRENT` se crea si no existe |

---

## 7. Integración con `finances-core`

- Usa `FinancesSettings` para todas las `FT_*` settings
- Usa `SQLTaskManager` de `finances-core` para persistencia de tareas
- Usa `finances_core.logging.configure_logging` y `finances_core.timeutils.parse_time_input`
- Usa el núcleo de descarga compartido `finances_core.marketdata` (escritor Hive sin pérdida, inventario, planes, ciclo de vida de tareas, reintentos, TokenBucket, meses), extraído de esta herramienta para reutilizarlo en `ft_market_data_yfinance` sin violar el Art. 4
- Su router lo monta `finances_api.create_app()` (`ft_serve`)
- Declara variables en `ft_setup/env_list.conf` (Art. 16)