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

```python
import typer
from typing import Optional, List
from pathlib import Path

app = typer.Typer(
    name="ft_market_data_binance",
    help="Descarga OHLCV histórica de Binance (Public Data + REST fallback)",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

@app.command()
def download(
    symbols: List[str] = typer.Option(..., "--symbol", "-s", help="Símbolos (ej. BTCUSDT ETHUSDT)"),
    timeframes: List[str] = typer.Option(..., "--timeframe", "-t", help="Timeframes (ej. 1h 5m 1d)"),
    start: str = typer.Option(..., "--start", help="Inicio (ISO 8601 o ms epoch): 2024-01-01 o 1704067200000"),
    end: Optional[str] = typer.Option(None, "--end", help="Fin (ISO 8601 o ms epoch), default=ahora"),
    market_type: str = typer.Option("spot", "--market", "-m", help="spot | futures"),
    max_concurrent: Optional[int] = typer.Option(None, "--max-concurrent", "-c", help="Descargas simultáneas (default: FT_DOWNLOAD_MAX_CONCURRENT=5)"),
    limit: int = typer.Option(1000, "--limit", "-l", help="Velas por request REST (1-1000)"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-descargar particiones existentes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo generar plan, no ejecutar"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: str = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """Descarga OHLCV determinística: Public Data prioritario, REST fallback."""

@app.command()
def plan(
    task_id: str = typer.Argument(..., help="UUID de tarea previa"),
    format: str = typer.Option("json", "--format", "-f", help="json | yaml"),
) -> None:
    """Muestra plan de descarga guardado para task_id."""

@app.command()
def status(
    task_id: str = typer.Argument(..., help="UUID de tarea"),
) -> None:
    """Consulta estado de tarea (polling local)."""

@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: str = typer.Option("rich", "--log-format"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
```

### 2.2 API (FastAPI) — `packages/finances-downloader/src/finances_downloader/ft_market_data_binance/api.py`

```python
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Path, status
from typing import Optional, List

router = APIRouter(prefix="/market-data/binance", tags=["market-data-binance"])

@router.post("/download", status_code=status.HTTP_202_ACCEPTED)
async def download_endpoint(
    background_tasks: BackgroundTasks,
    symbols: List[str] = Query(..., min_length=1),
    timeframes: List[str] = Query(..., min_length=1),
    start_ms: int = Query(..., gt=0),
    end_ms: Optional[int] = Query(None, gt=0),
    market_type: str = Query("spot", pattern="^(spot|futures)$"),
    max_concurrent: Optional[int] = Query(None, ge=1, le=50),
    limit_per_request: int = Query(1000, ge=1, le=1000),
) -> dict:
    """
    Inicia descarga en background.
    Retorna: {"task_id": "<uuid-v7>", "status": "ACCEPTED", "poll_url": "/market-data/binance/status/<task_id>"}
    """

@router.get("/status/{task_id}")
async def status_endpoint(task_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$")) -> DownloadProgress:
    """Progreso granular de descarga."""

@router.get("/plan/{task_id}")
async def plan_endpoint(task_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$")) -> DownloadPlan:
    """Plan de descarga generado (para debug/inspección)."""

@router.get("/result/{task_id}")
async def result_endpoint(task_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$")) -> DownloadResult:
    """Resultado final con rutas Parquet y stats."""
```

---

## 3. Algoritmos Clave

### 3.1 `parse_time_input(s: str) -> int` — Parse flexible a ms UTC
```python
# Acepta: "2024-01-01", "2024-01-01T00:00:00Z", "1704067200000", "now"
# Retorna: int64 ms UTC
```

### 3.2 `inventory_local_parquet(parquet_dir: Path, symbol: str, tf: str, market: str) -> List[Tuple[int, int]]`
```python
# Escanea particiones Hive existentes
# Retorna lista de rangos (min_open_time_ms, max_open_time_ms) por partición año/mes
# Usa Polars scan_parquet para leer solo metadatos (statistics) sin cargar datos
```

### 3.3 `compute_gaps(requested_start: int, requested_end: int, local_ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]`
```python
# Algoritmo: merge intervals + complemento
# 1. Ordenar local_ranges por start
# 2. Merge overlapping/adjacent
# 3. Recorrer desde requested_start hasta requested_end, emitir gaps
```

### 3.4 `check_public_data_availability(gaps: List[Tuple[int, int]], symbol: str, tf: str, market: str) -> List[DownloadPlanItem]`
```python
# Para cada gap:
#   - Determinar meses cubiertos (gap_start_ms -> gap_end_ms)
#   - Para cada mes: construir URL Public Data mensual
#     spot:   https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/{TF}/{SYMBOL}-{TF}-{YYYY-MM}.zip
#     futures: https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/{TF}/{SYMBOL}-{TF}-{YYYY-MM}.zip
#   - HEAD request para verificar existencia + obtener checksum (si disponible en headers)
#   - Agregar items source="public_data" para archivos existentes
#   - Sub-rangos sin archivo -> marcar para REST fallback
```

### 3.5 `plan_rest_fallback(uncovered_gaps: List[Tuple[int, int]], symbol: str, tf: str, market: str, limit: int) -> List[DownloadPlanItem]`
```python
# Para cada gap no cubierto:
#   - Paginar con limit=1000 (máx Binance)
#   - startTime=gap_start, endTime=min(gap_start + limit*tf_ms, gap_end)
#   - Generar items source="rest_api" con endpoint y params
```

### 3.6 `normalize_binance_klines(df: pl.DataFrame, source: Literal["public_data", "rest_api"]) -> pl.DataFrame`
```python
# Public Data CSV columns: open_time, open, high, low, close, volume, close_time, quote_asset_volume, number_of_trades, taker_buy_base_asset_volume, taker_buy_quote_asset_volume, ignore
# REST JSON array: [open_time, open, high, low, close, volume, close_time, quote_asset_volume, number_of_trades, taker_buy_base_asset_volume, taker_buy_quote_asset_volume, ignore]

# Transformaciones:
# 1. Renombrar columnas a esquema canónico
# 2. Cast: open_time -> open_time_ms (Int64), precios/volúmenes -> Float32, trades -> Int64
# 3. Agregar columnas: symbol, timeframe, market_type, year, month (derivadas de open_time_ms)
# 4. Filtrar: open_time_ms dentro del rango solicitado (por si CSV trae extra)
# 5. Ordenar por open_time_ms, unique (dedup por open_time_ms)
# 6. Validar schema Arrow vs OHLCV_SCHEMA
```

### 3.7 `write_parquet_partitioned(df: pl.DataFrame, parquet_dir: Path) -> List[str]`
```python
# Para cada grupo (market_type, symbol, timeframe, year, month):
#   path = parquet_dir / "binance" / f"market_type={m}" / f"symbol={s}" / f"timeframe={tf}"
#          / f"year={y}" / f"month={mm:02d}" / "data.parquet"
#   SI path existe: df = concat([read_parquet(path), df]).unique("open_time_ms", keep="last")
#   df.sort("open_time_ms").write_parquet(tmp, compression="zstd", statistics=True)
#   os.replace(tmp, path)   # reemplazo atómico
# Retorna lista de rutas de archivos escritos
```

### 3.8 Orquestador Principal — `execute_download_plan(plan: DownloadPlan, settings, semaphore) -> DownloadResult`
```python
async def execute_download_plan(plan: DownloadPlan):
    1. task_id = plan.task_id
    2. UPDATE task status=PLANNING
    3. Agrupar plan.items por source (public_data vs rest_api)
    4. Para cada grupo:
         - UPDATE status=DOWNLOADING_PUBLIC / DOWNLOADING_REST
         - Crear tasks asyncio con semáforo (max_concurrent)
         - Cada task: download -> validate checksum -> normalize -> collect DataFrames
    5. UPDATE status=NORMALIZING
    6. Concatenar todos los DataFrames normalizados por (symbol, timeframe, market_type)
    7. UPDATE status=WRITING_PARQUET
    8. write_parquet_partitioned por grupo
    9. UPDATE status=COMPLETED con stats y parquet_paths
    10. RETURN DownloadResult
```

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
│           ├── client.py           # Cliente HTTP dual (Public Data + REST) + TokenBucket
│           ├── planner.py          # Lógica pasos 1-6 (inventory, gaps, plan)
│           ├── normalizer.py       # normalize_binance_klines
│           ├── writer.py           # write_parquet_partitioned
│           ├── inventory.py        # inventory_local_parquet, compute_gaps
│           ├── public_data.py      # check_public_data_availability
│           ├── rest_api.py         # plan_rest_fallback, REST client
│           └── models.py           # Pydantic models (DownloadRequest, Plan, etc.)
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
| ADR-B08 | Sobrescribir partición completa (año/mes) vs append | Idempotencia simple; evita duplicados por re-ejecución |
| ADR-B09 | `finances-downloader` package separado de `finances-core` | Ciclo SemVer independiente; aislamiento de deps pesadas (httpx, polars) |
| ADR-B10 | Auto-registro de variables `FT_*` no declaradas (Art. 16 Constitución) | `FT_DOWNLOAD_MAX_CONCURRENT` se crea si no existe |

---

## 7. Integración con `finances-core`

- Usa `FinancesSettings` para todas las `FT_*` settings
- Usa `SQLTaskManager` de `finances-core` para persistencia de tareas
- Usa `configure_logging` patrón compartido
- Registra router en `ft_api_service` via `include_router(binance_router)`
- Declara variables en `ft_setup/env_list.conf` (Art. 16)