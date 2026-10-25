# Plan — `ft_data_viewer` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquemas Core (`packages/finances-core/schemas/data_viewer.py`)

```python
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Dict, Any
import polars as pl

class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"

class DatasetIdentifier(BaseModel):
    exchange: str = "binance"
    market_type: Literal["spot", "futures"]
    symbol: str
    timeframe: str

class DatasetInfo(BaseModel):
    id: DatasetIdentifier
    year_range: tuple[int, int]  # (min_year, max_year)
    month_count: int
    estimated_rows: int
    size_bytes: int
    last_modified_ms: int
    partition_path: str

class DatasetListResponse(BaseModel):
    datasets: List[DatasetInfo]
    total_count: int
    total_size_bytes: int
    total_rows: int

class ColumnStats(BaseModel):
    name: str
    dtype: str
    null_count: int
    distinct_count: Optional[int] = None
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    std: Optional[float] = None

class DatasetSummary(BaseModel):
    dataset_id: DatasetIdentifier
    row_count: int
    date_range: tuple[int, int]
    columns: List[ColumnStats]
    gaps_detected: int
    duplicate_timestamps: int
    outlier_count: int

class GapInfo(BaseModel):
    gap_start_ms: int
    gap_end_ms: int
    missing_candles: int
    gap_duration_human: str

class AnomalyReport(BaseModel):
    dataset_id: DatasetIdentifier
    duplicate_timestamps: Dict[str, Any]
    null_values: Dict[str, int]
    price_outliers: Dict[str, List[Dict]]
    volume_anomalies: Dict[str, List[Dict]]

class ExportRequest(BaseModel):
    dataset_id: DatasetIdentifier
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    columns: Optional[List[str]] = None
    format: Literal["parquet", "csv", "jsonl"] = "parquet"
    output_dir: Optional[str] = None

class CompareRequest(BaseModel):
    dataset_a: DatasetIdentifier
    dataset_b: DatasetIdentifier
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

class CompareResult(BaseModel):
    dataset_a: DatasetIdentifier
    dataset_b: DatasetIdentifier
    row_count_diff: int
    date_range_overlap: Optional[tuple[int, int]]
    price_divergence_max: float
    volume_divergence_pct: float
    gaps_only_in_a: List[GapInfo]
    gaps_only_in_b: List[GapInfo]
```

### 1.2 Extensión de Configuración (`packages/finances-core/src/finances_core/config.py`)

```python
# Añadir a FinancesSettings:
DATA_VIEWER_SAMPLE_MAX_ROWS: int = 10000
DATA_VIEWER_GAP_THRESHOLD_MS: int = 0
```

### 1.3 Actualización `env_list.conf.j2` (Art. 16 Constitución)

```toml
[[vars]]
name = "FT_DATA_VIEWER_SAMPLE_MAX_ROWS"
description = "Máximo filas para sampling aleatorio en datasets grandes"
example = "10000"
required = false
var_type = "int"
secret = false
source_tool = "ft_data_viewer"
since_version = "0.1.0"

[[vars]]
name = "FT_DATA_VIEWER_GAP_THRESHOLD_MS"
description = "Umbral en ms para reportar gaps (0 = todos)"
example = "0"
required = false
var_type = "int"
secret = false
source_tool = "ft_data_viewer"
since_version = "0.1.0"
```

---

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-downloader/src/finances_downloader/ft_data_viewer/cli.py`

```python
app = typer.Typer(
    name="ft_data_viewer",
    help="Explorador y diagnóstico de datasets OHLCV",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

@app.command()
def list(
    exchange: Optional[str] = typer.Option(None, "--exchange", "-e"),
    market_type: Optional[str] = typer.Option(None, "--market", "-m"),
    symbol: Optional[str] = typer.Option(None, "--symbol", "-s"),
    timeframe: Optional[str] = typer.Option(None, "--timeframe", "-t"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Lista datasets OHLCV disponibles en FT_PARQUET_DIR."""

@app.command()
def summary(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Resumen estadístico de un dataset (metadata-only)."""

@app.command()
def head(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(10, "--n", "-n", min=1, max=10000),
    cols: Optional[str] = typer.Option(None, "--cols", help="Columnas separadas por coma: open,high,low,close,volume"),
    format: Literal["table", "json", "csv"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Primeras N filas ordenadas por open_time_ms ASC."""

@app.command()
def tail(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(10, "--n", "-n", min=1, max=10000),
    cols: Optional[str] = typer.Option(None, "--cols"),
    format: Literal["table", "json", "csv"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Últimas N filas ordenadas por open_time_ms DESC."""

@app.command()
def sample(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(100, "--n", "-n", min=1, max=10000),
    cols: Optional[str] = typer.Option(None, "--cols"),
    format: Literal["table", "json", "csv"] = typer.Option("table", "--format", "-f"),
) -> None:
    """N filas aleatorias (reservoir sampling)."""

@app.command()
def gaps(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    min_gap_candles: int = typer.Option(1, "--min-gap", min=1),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Detecta gaps temporales en el dataset."""

@app.command()
def anomalies(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Detecta duplicados, nulos, outliers de precio/volumen."""

@app.command()
def export(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    start: Optional[str] = typer.Option(None, "--start"),
    end: Optional[str] = typer.Option(None, "--end"),
    cols: Optional[str] = typer.Option(None, "--cols"),
    format: Literal["parquet", "csv", "jsonl"] = typer.Option("parquet", "--format", "-f"),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", "-o", help="Directorio salida (default: stdout)"),
) -> None:
    """Exporta subconjunto filtrado a Parquet/CSV/JSONL."""

@app.command()
def compare(
    exchange_a: str = typer.Option(..., "--exchange-a"),
    market_type_a: str = typer.Option(..., "--market-a"),
    symbol_a: str = typer.Option(..., "--symbol-a"),
    timeframe_a: str = typer.Option(..., "--timeframe-a"),
    exchange_b: str = typer.Option(..., "--exchange-b"),
    market_type_b: str = typer.Option(..., "--market-b"),
    symbol_b: str = typer.Option(..., "--symbol-b"),
    timeframe_b: str = typer.Option(..., "--timeframe-b"),
    start: Optional[str] = typer.Option(None, "--start"),
    end: Optional[str] = typer.Option(None, "--end"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Compara dos datasets (mismo symbol/tf o distinto exchange/market)."""
```

### 2.2 API (FastAPI) — `packages/finances-downloader/src/finances_downloader/ft_data_viewer/api.py`

```python
router = APIRouter(prefix="/data", tags=["data-viewer"])

@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets(
    exchange: Optional[str] = None,
    market_type: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> DatasetListResponse: ...

@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/summary", response_model=DatasetSummary)
async def get_summary(
    exchange: str, market_type: str, symbol: str, timeframe: str
) -> DatasetSummary: ...

@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/rows")
async def get_rows(
    exchange: str, market_type: str, symbol: str, timeframe: str,
    mode: Literal["head", "tail", "sample"] = Query("head"),
    n: int = Query(10, ge=1, le=10000),
    cols: Optional[str] = None,
) -> List[Dict]: ...

@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/gaps", response_model=List[GapInfo])
async def get_gaps(
    exchange: str, market_type: str, symbol: str, timeframe: str,
    min_gap_candles: int = Query(1, ge=1),
) -> List[GapInfo]: ...

@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/anomalies", response_model=AnomalyReport)
async def get_anomalies(
    exchange: str, market_type: str, symbol: str, timeframe: str
) -> AnomalyReport: ...

@router.post("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export")
async def export_data(
    exchange: str, market_type: str, symbol: str, timeframe: str,
    request: ExportRequest,
) -> StreamingResponse: ...

@router.post("/compare", response_model=CompareResult)
async def compare_datasets(request: CompareRequest) -> CompareResult: ...
```

---

## 3. Algoritmos Clave

| Función | Descripción |
|---------|-------------|
| `scan_parquet_partitions(parquet_dir)` | `pl.scan_parquet(parquet_dir / "**/*.parquet", hive_partitioning=True).collect_metadata()` → lista `DatasetInfo` sin leer datos |
| `read_dataset_metadata(dataset_path)` | `pl.scan_parquet(path).select(pl.all().meta()).collect()` → stats por columna (min, max, null_count, distinct_count) |
| `detect_gaps(lf, timeframe_ms)` | `lf.sort("open_time_ms").with_columns(diff=pl.col("open_time_ms").diff()).filter(pl.col("diff") > timeframe_ms * 1.5)` |
| `detect_anomalies(lf)` | Null check, `duplicated()`, Z-score `(x - mean) / std > 3`, IQR `Q1 - 1.5*IQR / Q3 + 1.5*IQR`, volumen == 0 |
| `stream_export(lf, format, output)` | `lf.sink_parquet()/sink_csv()/sink_ndjson()` streaming sin cargar en memoria |
| `compare_datasets(lf_a, lf_b)` | Join on `open_time_ms`, diff en `close`, `volume`, union de gaps |

---

## 4. Estructura de Archivos Objetivo

```
packages/finances-downloader/
├── src/finances_downloader/ft_data_viewer/
│   ├── __init__.py
│   ├── cli.py              # Typer app (9 comandos)
│   ├── api.py              # FastAPI router (7 endpoints)
│   ├── service.py          # Orquestador compartido
│   ├── scanner.py          # scan_parquet_partitions, metadata
│   ├── analyzer.py         # gaps, anomalies, stats
│   ├── exporter.py         # stream_export (sink_*)
│   ├── comparator.py       # compare_datasets
│   └── models.py           # Re-exports desde finances_core
```

---

## 5. Dependencias (`packages/finances-downloader/pyproject.toml`)

```toml
[project]
name = "finances-downloader"
version = "0.1.0"
dependencies = [
    "finances-core @ file:///c/P/financial_tools/packages/finances-core",
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
    "tenacity>=8.2",
]

[project.scripts]
ft_market_data_binance = "finances_downloader.ft_market_data_binance.cli:app"
ft_data_viewer = "finances_downloader.ft_data_viewer.cli:app"
```

---

## 6. Decisiones Arquitectónicas Clave (ADR)

| ADR | Decisión | Justificación |
|-----|----------|---------------|
| DV-01 | **Solo lectura**: `ft_data_viewer` nunca escribe en `FT_PARQUET_DIR`. Exporta a directorio separado. | Previene corrupción accidental; separación de responsabilidades |
| DV-02 | **Lazy Polars**: Todo uso de `pl.scan_parquet` + `collect()` solo cuando necesario. Sin `read_parquet` completo. | Escalabilidad a datasets > RAM; aprovecha predicate pushdown y partition pruning |
| DV-03 | **Streaming export**: `sink_parquet/csv/jsonl` para exportar datasets > memoria RAM. | Sin OOM; procesamiento chunked |
| DV-04 | **Metadata-first**: `summary`, `gaps`, `anomalies` usan statistics de Parquet (min/max/null_count) sin leer filas. | Velocidad extrema en diagnosis; I/O mínimo |
| DV-05 | **TF_MS_MAP compartido**: Reutiliza `finances_core.schemas.market_data.TF_MS_MAP` para gap detection. | Single source of truth; consistencia con downloader |
| DV-06 | **Reservoir sampling**: Para `sample N` en datasets grandes, uso de algoritmo Vitter R. | Muestra uniforme sin leer todo el dataset |
| DV-07 | **Dualidad CLI/API real**: Mismo `service.py` llamado desde Typer y FastAPI. | Art. 1 Constitución; evita duplicación de lógica |

---

## 7. Integración con `finances-core` y `finances-downloader`

- Reutiliza `FinancesSettings` para `FT_PARQUET_DIR`, `FT_DATA_VIEWER_*`
- Reutiliza `TF_MS_MAP`, `OHLCV_SCHEMA`, `OHLCV_PARTITION_COLS` de `finances_core.schemas.market_data`
- Reutiliza `configure_logging` patrón compartido
- Registra router en `ft_api_service` via `include_router(data_viewer_router)`
- Declara variables en `ft_setup/env_list.conf` (Art. 16)