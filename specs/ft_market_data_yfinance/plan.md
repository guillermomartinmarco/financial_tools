# Plan — `ft_market_data_yfinance` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquemas (`packages/finances-core/src/finances_core/schemas/market_data_yfinance.py`)
```python
YFTimeframe = Literal["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"]
YFPhase = Literal["PENDING", "PLANNING", "DOWNLOADING", "NORMALIZING",
                  "WRITING_PARQUET", "COMPLETED", "FAILED"]

class YFDownloadRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=100)   # tal cual Yahoo: AAPL, ^GSPC, EURUSD=X, GC=F
    timeframes: list[YFTimeframe] = Field(min_length=1)
    start_ms: int
    end_ms: int | None = None                                   # None = now
    max_concurrent: int | None = None                           # None = FT_DOWNLOAD_MAX_CONCURRENT

class YFPlanItem(BaseModel):
    symbol: str
    timeframe: YFTimeframe
    yahoo_interval: str                                         # 1m, 5m, ..., 1wk, 1mo
    start_ms: int
    end_ms: int                                                 # exclusivo

class YFUnfillableGap(BaseModel):
    symbol: str
    timeframe: YFTimeframe
    start_ms: int
    end_ms: int
    reason: Literal["beyond_yahoo_lookback"]

class YFDownloadPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: list[YFPlanItem]
    gaps_unfillable: list[YFUnfillableGap]

class YFDownloadProgress(BaseModel):
    task_id: str
    status: YFPhase
    items_total: int
    items_done: int
    processed_candles: int
    events_written: int
    gaps_found: int
    errors: list[str]

class YFDownloadResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    parquet_paths: list[str]
    events_paths: list[str]
    stats: dict[str, object]           # por símbolo/timeframe: candles, market_type
    errors: list[str]
    gaps_unfillable: list[YFUnfillableGap]

class DispatchResponse(BaseModel):     # igual que binance
    task_id: str
    status: Literal["ACCEPTED"]
    poll_url: str
    plan_url: str
    result_url: str
```

### 1.2 Esquemas Polars
```python
YF_OHLCV_SCHEMA = {
    "symbol": pl.Utf8, "timeframe": pl.Utf8, "market_type": pl.Utf8,
    "open_time_ms": pl.Int64,
    "open": pl.Float32, "high": pl.Float32, "low": pl.Float32, "close": pl.Float32,
    "adj_close": pl.Float32, "volume": pl.Float32,
}
YF_EVENTS_SCHEMA = {"event_time_ms": pl.Int64, "event_type": pl.Utf8, "value": pl.Float64}
INSTRUMENT_TYPE_MAP = {"EQUITY": "equity", "ETF": "etf", "INDEX": "index", "CURRENCY": "fx",
                       "CRYPTOCURRENCY": "crypto", "FUTURE": "future", "MUTUALFUND": "fund"}
```

### 1.3 Configuración (`finances_core/config.py`)
```python
YFINANCE_RATE_CAPACITY: int = 5
YFINANCE_RATE_REFILL_RATE: float = 1.0
YFINANCE_MAX_RETRIES: int = 3
YFINANCE_BASE_BACKOFF_MS: int = 2000
YFINANCE_TIMEOUT_S: int = 30
# Se elimina YFINANCE_API_KEY (Yahoo no usa key).
```

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-downloader/src/finances_downloader/ft_market_data_yfinance/cli.py`
```
ft_market_data_yfinance [-v] [-q] [--log-format rich|json] COMMAND
  download  -s/--symbol SYMBOL... -t/--timeframe TF... --start T [--end T]
            [--max-concurrent N] [--dry-run]
  plan      TASK_ID        # JSON == GET /plan/{task_id}
  status    TASK_ID        # JSON == GET /status/{task_id}
  result    TASK_ID        # JSON == GET /result/{task_id}
Exit: 0 OK · 1 no encontrado/genérico · 2 validación · 3 tarea FAILED · 4 IO
```
`--start/--end` aceptan lo mismo que `finances_core.timeutils.parse_time_input`.

### 2.2 API (FastAPI) — `.../ft_market_data_yfinance/api.py`, `router = APIRouter(prefix="/market-data/yfinance")`
| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/download` (query: `symbols`, `timeframes`, `start`, `end`, `max_concurrent`) | `202` `DispatchResponse` |
| GET | `/status/{task_id}` | `YFDownloadProgress` · 404 |
| GET | `/plan/{task_id}` | `YFDownloadPlan` · 404 |
| GET | `/result/{task_id}` | `YFDownloadResult` · 404 · 409 si no terminó |

`task_id` validado con `^[0-9a-f-]{36}$`. Montado por `finances_api.create_app()`.

## 3. Algoritmos Clave

### 3.1 Límites y chunking — `timeframes.py`
```python
YAHOO_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                  "1h": "1h", "1d": "1d", "1w": "1wk", "1M": "1mo"}
LOOKBACK_DAYS = {"1m": 30, "5m": 60, "15m": 60, "30m": 60, "1h": 730}   # ausente = sin límite
MAX_WINDOW_DAYS = {"1m": 8, "5m": 60, "15m": 60, "30m": 60, "1h": 730}  # ausente = un pedido
def split_by_limits(gap, timeframe, now_ms) -> tuple[list[window], list[unfillable]]
```
Margen de seguridad: el límite de historial se aplica con 1 hora de holgura para no rozar el borde de Yahoo.

Duración de vela de calendario (velas a medianoche local de la bolsa): "+1 timeframe" de RF-1/RF-6 se
aplica con la duración **mínima** real (`1d` = 23 h por DST, `1w` = 7 d − 1 h, `1M` = 28 d) para que la
siguiente vela nunca se dé por cubierta sin tenerla; un gap interno más corto que esa duración (p. ej.
`última + 23 h` → medianoche siguiente en un cambio de mes) no puede contener una vela y no se pide.
La vela aún en formación (`open + duración máxima > now`: 25 h, 7 d + 1 h, 31 d) no se persiste, para
que la re-ejecución la traiga completa en lugar de congelar un parcial.
`EVENT_TIMEFRAMES = {1m..1d}`: en `1wk`/`1mo` yfinance asigna las acciones corporativas a la fila de la
semana/mes, así que esos timeframes no alimentan la tabla de eventos.

### 3.2 Planificación — `planner.py`
1. `validate_request` (núcleo compartido) + timeframes RF-2.
2. Para cada (symbol, tf): `inventory_local_parquet(parquet_dir, exchange="yfinance", symbol, tf, market_type="*")` → `compute_gaps`.
3. `split_by_limits` → `YFPlanItem` + `YFUnfillableGap`. Sin red: la planificación es pura y rápida (Art. 18).
4. Persistir con `save_plan` del núcleo.

### 3.3 Adaptador Yahoo — `adapter.py` (único módulo que importa `yfinance`/`pandas`)
```python
@dataclass(frozen=True)
class YahooBatch:
    candles: pl.DataFrame          # columnas Yahoo ya pasadas a Polars, índice como open_time (UTC)
    events: pl.DataFrame           # event_time_ms, event_type, value (solo valores != 0)
    instrument_type: str | None    # history_metadata["instrumentType"]

def fetch_history(symbol: str, yahoo_interval: str, start_ms: int, end_ms: int,
                  timeout_s: int) -> YahooBatch   # síncrona
FetchFn = Callable[[str, str, int, int, int], YahooBatch]
def default_fetcher() -> FetchFn       # punto de inyección para tests (como default_transport de binance)
```
- `Ticker(symbol).history(interval=..., start=..., end=..., auto_adjust=False, actions=True, timeout=...)` con excepciones visibles (`yf.config.debug.hide_exceptions = False`; `raise_errors` está deprecado en 1.7).
- Clasificación de errores: `YFRateLimitError` y errores de red → reintentables; `YFPricesMissingError` → `SymbolNotFoundError` (no reintentable) si el mensaje indica "No data found", si no → ventana vacía.
  Medido en el smoke del 2026-10-25: un símbolo inexistente también puede llegar como `YFTzMissingError`
  ("no timezone found") → `SymbolNotFoundError`. Los errores de red de yfinance (curl_cffi) heredan de `OSError`.
- `event_time_ms` = medianoche local de la bolsa del día del evento (en ms UTC), igual en intradía y diario.
- Medido: con `auto_adjust=False` Yahoo devuelve OHLC **ajustado por splits** (no por dividendos);
  `adj_close` añade el ajuste por dividendos. AAPL 2020-08-07: `open` 113.2 (cotizó ~452 antes del 4:1).
- Se ejecuta con `asyncio.to_thread` dentro del servicio.

### 3.4 Normalización — `normalizer.py`
`normalize_yahoo_batch(batch, *, symbol, timeframe, start_ms, end_ms) -> pl.DataFrame` según RF-4 y `YF_OHLCV_SCHEMA`; filtra al rango `[start_ms, end_ms)`, descarta OHLC nulos, dedupe, orden, columnas `year`/`month` para la partición.

### 3.5 Eventos — `events.py`
`write_events(events, parquet_dir, symbol) -> str | None`: merge sin pérdida (dedupe por `event_time_ms`+`event_type`), escritura atómica con el escritor del núcleo.

### 3.6 Orquestador — `service.py`
Reusa `finances_core.marketdata.tasks_lifecycle` (recorder con `asyncio.to_thread`, fases monótonas, `run_download_sync`):
```
create_task (PENDING) → PLANNING (planner) → DOWNLOADING (fetch por item, semáforo + TokenBucket + retry)
→ NORMALIZING → WRITING_PARQUET (write_parquet_partitioned exchange="yfinance" + write_events)
→ COMPLETED | FAILED (solo si algún item agotó reintentos)
```
Los pedidos de un mismo símbolo van en secuencia (los símbolos en paralelo bajo el semáforo): tras un
`SymbolNotFoundError` o reintentos agotados se omiten las ventanas restantes de ese símbolo.
`stats["{symbol}/{timeframe}"] = {candles, market_type, requests}`.

## 4. Estructura de Archivos Objetivo
```
packages/finances-core/src/finances_core/
├── marketdata/                    # NUEVO (extraído de ft_market_data_binance, Art. 4)
│   ├── hive_writer.py             # partition_dir, merge sin pérdida, escritura atómica (exchange, schema)
│   ├── inventory.py               # inventory_local_parquet, compute_gaps (exchange, market_type="*")
│   ├── plans.py                   # new_task_id, validate_request, plan_file_path, load/save_plan
│   ├── tasks_lifecycle.py         # TaskRecorder, create/get progress/result, run_download_sync
│   ├── retry.py                   # build_retrying(RetryPolicy(max_retries, base_backoff_ms), is_retryable, log_event)
│   ├── rate_limit.py              # TokenBucket
│   └── months.py                  # helpers de meses
└── schemas/market_data_yfinance.py
packages/finances-downloader/src/finances_downloader/ft_market_data_yfinance/
├── __init__.py
├── adapter.py  timeframes.py  planner.py  normalizer.py  events.py  service.py  api.py  cli.py
packages/finances-downloader/tests/ft_market_data_yfinance/
├── conftest.py                    # FakeYahoo: fetcher inyectado, sin red
└── test_*.py
```

## 5. Dependencias (pyproject.toml — finances-downloader)
- `yfinance>=1.7` (arrastra `pandas`; se usa solo en `adapter.py`).
- Script: `ft_market_data_yfinance = "finances_downloader.ft_market_data_yfinance.cli:app"`.
- mypy: override `ignore_missing_imports` para `yfinance.*` (sin stubs); `pandas` tipado vía `pandas-stubs` en `dev`.

## 6. Decisiones Arquitectónicas Clave (ADR)
| ID | Decisión | Motivo |
|---|---|---|
| ADR-Y01 | `yfinance` como fuente | Resuelve cookies/crumb y cambios de Yahoo; no hay API oficial |
| ADR-Y02 | pandas solo en `adapter.py`; Polars en el resto | Constitución: Polars > pandas; el borde se aísla y se testea con un fetcher falso |
| ADR-Y03 | Precios crudos + `adj_close` + tabla de eventos | El ajuste de Yahoo cambia retroactivamente; los crudos y eventos son inmutables y permiten cualquier ajuste |
| ADR-Y04 | `market_type` detectado de `instrumentType` | Gratis en cada respuesta; el usuario no lo tiene que saber |
| ADR-Y05 | Límites de Yahoo como tabla de constantes | Un único lugar para ajustar si Yahoo cambia sus límites |
| ADR-Y06 | Núcleo de descarga en `finances_core.marketdata` | Art. 4: binance y yfinance comparten escritor, inventario, tareas y reintentos sin importarse entre sí |

## 7. Integración con `finances-core` y la suite
- `finances_core.schemas.data_viewer.DatasetIdentifier.market_type` se amplía a `spot | futures | equity | etf | index | fx | crypto | future | fund | other` (spec de `ft_data_viewer`).
- `finances_api.create_app()` monta el router `/market-data/yfinance` (spec de `finances_api`).
- `ft_setup` `env_list.conf(.j2)`: variables `FT_YFINANCE_*` con `source_tool="ft_market_data_yfinance"`; se elimina `FT_YFINANCE_API_KEY`.
- `ft_market_data_binance` pasa a usar `finances_core.marketdata` (sin cambio de comportamiento; sus tests son la red de seguridad).
