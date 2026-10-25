# Spec — `ft_market_data_binance` (MVP v1.0)

## Contexto e Historias de Usuario

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** descargar datos OHLCV históricos de Binance (Spot y Futures) para múltiples símbolos y timeframes **para que** pueda construir datasets de backtesting completos y reproducibles. |
| US-2 | Data Engineer | Como ingeniero de datos, **necesito** una descarga **determinística, resumible e idempotente** que priorice **Binance Public Data** (data.binance.vision) y use **REST API como fallback** **para que** pipelines masivos sean rápidos, baratos y no saturen límites de API. |
| US-3 | Backtest Engineer | Como usuario de `ft_backtestopt_spectre` / `ft_vectorbt`, **exijo** que los datos lleguen ya normalizados al esquema canónico `float32`/`int64` (Art. 5 Constitución) **para que** no pierda tiempo limpiando/convertendo antes de computar. |
| US-4 | DevOps / Operador | Como operador, **quiero** un CLI `ft_market_data_binance download` y un endpoint `POST /market-data/binance/download` **para que** pueda lanzar descargas masivas desde terminal o desde la UI del `ft_api_service`, con control de concurrencia configurable. |

---

## Requisitos Funcionales (EARS)

### RF-1 — Pipeline de Descarga Determinista de 7 Pasos (Ubícuo)
**EL SISTEMA DEBE** ejecutar **en orden estricto** el siguiente pipeline para cada par `(symbol, timeframe, market_type)` solicitado:

1. **Validar Petición**: Verificar que `start_ms` ≤ `now_ms - 1 vela`, `end_ms` ≥ `start_ms`, timeframes válidos Binance. **SI** inválido, **ENTONCES** rechazo inmediato (exit 2 / HTTP 400) con detalle. La existencia de cada símbolo se verifica contra exchangeInfo durante la planificación: un símbolo inexistente no rechaza la petición, se trata según RF-9 (warning, se omite y se registra en `errors`).
2. **Inventario Local**: Escanear `FT_PARQUET_DIR/binance/{spot|futures}/{symbol}/timeframe={tf}/` y construir mapa de rangos cubiertos por partición (min/max `open_time_ms` por archivo Parquet).
3. **Determinar Huecos (Gaps)**: Comparar rango solicitado `[start_ms, end_ms]` vs rangos locales → lista de intervalos faltantes `[(gap_start_ms, gap_end_ms), ...]`.
4. **Verificar Binance Public Data**: Para cada gap, consultar índice público `https://data.binance.vision/?prefix=data/spot/monthly/klines/{SYMBOL}/{TF}/` (o `data/futures/um/monthly/klines/...`) y determinar **qué archivos .zip mensuales existen** y cubren el gap.
5. **Planificar Fallback REST**: Para sub-rangos de gaps **no cubiertos** por Public Data, planear requests a REST API (`GET /api/v3/klines` spot, `GET /fapi/v1/klines` futures) con paginación `limit=1000`.
6. **Generar Plan de Descarga**: Persistir `<raíz del proyecto>/share/plans/download_plan_{task_id}.json` (persistente, para que `GET /plan` lo encuentre después) con estructura:
   ```json
   {
     "task_id": "uuid-v7",
     "created_at_ms": 1700000000000,
     "items": [
       {
         "source": "public_data",
         "symbol": "BTCUSDT",
         "timeframe": "1h",
         "market_type": "spot",
         "url": "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip",
         "expected_checksum": "sha256:...",
         "covers_range_ms": [1704067200000, 1706745600000]
       },
       {
         "source": "rest_api",
         "symbol": "ETHUSDT",
         "timeframe": "5m",
         "market_type": "futures",
         "endpoint": "/fapi/v1/klines",
         "params": {"symbol": "ETHUSDT", "interval": "5m", "startTime": 1700000000000, "endTime": 1700086400000, "limit": 1000},
         "covers_range_ms": [1700000000000, 1700086400000]
       }
     ]
   }
   ```
7. **Ejecutar Plan**: Procesar items en paralelo (ver RF-2), descargar, validar checksum, normalizar a esquema canónico (RF-3), escribir Parquet particionado (RF-4), actualizar inventario local.

### RF-2 — Control de Concurrencia Configurable (Ubícuo)
**EL SISTEMA DEBE** permitir configurar descargas simultáneas:
- **Variable de entorno**: `FT_DOWNLOAD_MAX_CONCURRENT` (default: `5`)
- **Flag CLI**: `--max-concurrent N` (sobrescribe env var)
- **API**: query param `max_concurrent` (sobrescribe env var)
- **Comportamiento**: Semáforo `asyncio.Semaphore(N)` para descargas HTTP concurrentes (tanto Public Data como REST). **SI** variable no existe en entorno, **ENTONCES** crear en proceso con default `5` y loguear info "Usando default FT_DOWNLOAD_MAX_CONCURRENT=5".

### RF-3 — Normalización Canónica Inmediata (Ubícuo)
**EL SISTEMA DEBE** transformar **cada vela** (ya venga de Public Data CSV o REST JSON) al esquema canónico **antes de persistir**:
```python
# Esquema canónico (Art. 5 Constitución) — Polars DataFrame dtypes:
{
    "symbol": str,                    # "BTCUSDT"
    "timeframe": str,                 # "1h"
    "market_type": str,               # "spot" | "futures"
    "open_time_ms": int64,            # int64 ms UTC — apertura de vela
    "open": float32,                  # float32
    "high": float32,
    "low": float32,
    "close": float32,
    "volume": float32,                # volumen en asset base (BTC)
    "quote_volume": float32,          # volumen en asset quote (USDT)
    "trades_count": int64,            # número de trades en la vela
    "taker_buy_base_volume": float32,
    "taker_buy_quote_volume": float32,
}
```
**NINGÚN** dato crudo (strings de CSV, arrays JSON de REST) debe cruzar la frontera del módulo sin pasar por `normalize_binance_klines(df: pl.DataFrame, source: Literal["public_data","rest_api"]) -> pl.DataFrame`.

### RF-4 — Persistencia en Parquet Particionado Hive (Ubícuo)
**EL SISTEMA DEBE** escribir datos normalizados en `FT_PARQUET_DIR` con particionado:
```
{FT_PARQUET_DIR}/
  binance/
    market_type=spot/
      symbol=BTCUSDT/
        timeframe=1h/
          year=2024/
            month=01/
              data.parquet
    market_type=futures/
      symbol=ETHUSDT/
        timeframe=5m/
          ...
```
- Particionado Hive completo (`clave=valor` en todos los niveles bajo `binance/`), de modo que `pl.scan_parquet(..., hive_partitioning=True)` infiera `market_type`, `symbol`, `timeframe`, `year` y `month`. El inventario local (paso 2 de RF-1) y `ft_data_viewer` **DEBEN** leer exactamente este layout.
- Cada archivo Parquet **DEBE** tener schema Arrow: `Float32` para precios/volúmenes, `Int64` para timestamps/counts
- **Idempotencia sin pérdida**: **SI** la partición (año/mes) ya existe, **ENTONCES** el sistema **DEBE** combinar las filas existentes con las nuevas, deduplicar por `open_time_ms` (prevalece la fila nueva), ordenar por `open_time_ms` y reescribir la partición de forma atómica (archivo temporal + reemplazo). Nunca append row-a-row y nunca descartar filas existentes fuera del rango descargado.

### RF-5 — Rate-Limiting Diferenciado por Fuente (Ubícuo)
| Fuente | Algoritmo | Configuración (`FT_` settings) |
|--------|-----------|--------------------------------|
| **Binance Public Data** | Token Bucket **generoso** (descarga masiva sin API key) | `FT_BINANCE_PUBLIC_DATA_RATE_CAPACITY` (default 50), `FT_BINANCE_PUBLIC_DATA_RATE_REFILL` (default 10/s) |
| **Binance REST API** | Token Bucket **estricto** (límites oficiales) | `FT_BINANCE_REST_RATE_CAPACITY` (default 1200 weight/min), `FT_BINANCE_REST_RATE_REFILL` (default 20/s), `FT_BINANCE_MAX_RETRIES` (default 3; reintentos adicionales tras el primer intento, solo ante `418`/`429`/`5xx`/red), `FT_BINANCE_BASE_BACKOFF_MS` (default 1000) |

**Backoff exponencial + jitter** para ambas ante `429`/`418`/`5xx`. Log estructurado por retry (Art. 9).

### RF-6 — Registro de Progreso en SQLTaskManager (Estado-Driven)
**EL SISTEMA DEBE** persistir estado en `share/tasks.db` (WAL) con `task_id` (UUID v7):
- Estados: `PENDING` → `PLANNING` → `DOWNLOADING_PUBLIC` → `DOWNLOADING_REST` → `NORMALIZING` → `WRITING_PARQUET` → `COMPLETED` / `FAILED`
- Campos: `downloaded_bytes`, `processed_candles`, `gaps_found`, `gaps_filled_public`, `gaps_filled_rest`, `errors_json`
- Polling via `GET /market-data/binance/status/{task_id}` (Art. 18)

### RF-7 — Modo Servicio: Background Task + Polling (Ubícuo)
**EL SISTEMA DEBE** exponer bajo FastAPI (prefijo `/market-data/binance`):
- `POST /download` → Valida, genera plan, inicia en `BackgroundTasks`, retorna `task_id` + `poll_url`
- `GET /status/{task_id}` → Progreso granular por fase
- `GET /plan/{task_id}` → Retorna `download_plan_{task_id}.json` para inspección/debug
- `GET /result/{task_id}` → Resumen final + rutas Parquet generadas

### RF-8 — Logging Dual y Métricas (Ubícuo)
- **CLI**: Rich progress bars por fase (Planning, Public Data, REST, Normalize, Write), tabla final con stats por símbolo/timeframe
- **API**: JSON estructurado con `task_id`, `phase`, `symbol`, `timeframe`, `source`, `items_total`, `items_done`, `bytes_downloaded`, `duration_ms`
- Métricas: throughput (MB/s, velas/s), ratio Public Data vs REST, latency p95/p99 REST

### RF-9 — Manejo de Errores Específicos (Evento-Driven)
| Situación | Acción |
|-----------|--------|
| Datos futuros solicitados (`start_ms > now_ms - 1 vela`) | Error 400/CLI exit 2: "Cannot download future data" |
| Símbolo no existe en exchangeInfo | Warning, saltar símbolo, continuar |
| Archivo Public Data corrupto (checksum mismatch) | Reintentar 1 vez; si falla → marcar gap para REST fallback |
| REST API `429`/`418` | Backoff exponencial + jitter, retry hasta `FT_BINANCE_MAX_RETRIES` |
| Gap sin cobertura ni en Public Data ni en REST (datos no existen en Binance) | Log warning, registrar en `gaps_unfillable` en resultado, continuar |
| Red/Timeout/HTTP agotado retries | Marcar task `FAILED` con error detallado; datos ya escritos en Parquet persisten (idempotencia). Un gap para el que Binance no devuelve datos **no** es `FAILED`: va a `gaps_unfillable` y la tarea termina `COMPLETED` |
| Errores por item (cualquier fila de esta tabla) | Se acumulan en `DownloadResult.errors` (y en `DownloadProgress.errors` durante la ejecución) |

### RF-10 — Configuración vía `FT_` Settings (Ubícuo) — **Nuevas Variables**
**EL SISTEMA DEBE** leer de `FinancesSettings` (prefijo `FT_`):
```python
# Binance Public Data
FT_BINANCE_PUBLIC_DATA_BASE_URL_SPOT: str = "https://data.binance.vision/data/spot"
FT_BINANCE_PUBLIC_DATA_BASE_URL_FUTURES: str = "https://data.binance.vision/data/futures/um"
FT_BINANCE_PUBLIC_DATA_RATE_CAPACITY: int = 50
FT_BINANCE_PUBLIC_DATA_RATE_REFILL_RATE: float = 10.0

# Binance REST API
FT_BINANCE_REST_BASE_URL_SPOT: str = "https://api.binance.com"
FT_BINANCE_REST_BASE_URL_FUTURES: str = "https://fapi.binance.com"
FT_BINANCE_REST_RATE_CAPACITY: int = 1200
FT_BINANCE_REST_RATE_REFILL_RATE: float = 20.0
FT_BINANCE_MAX_RETRIES: int = 3
FT_BINANCE_BASE_BACKOFF_MS: int = 1000

# Concurrencia
FT_DOWNLOAD_MAX_CONCURRENT: int = 5

# Almacenamiento (ya existían en ft_setup)
FT_PARQUET_DIR: pathlib.Path = pathlib.Path("./data/parquet")
FT_SHARE_DIR: pathlib.Path = pathlib.Path("./share")
```

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **WebSockets / Datos en tiempo real** | **Fuera de alcance MVP**. Solo OHLCV histórico. RFC futuro `ft_market_data_binance_ws`. |
| **Funding rates, open interest, liquidations** | **Fuera de alcance MVP**. Solo klines/OHLCV. |
| **Otros formatos (CSV, HDF5, Zarr)** | **Fuera de alcance MVP**. Solo Parquet particionado. |
| **Merge multi-exchange** | **Fuera de alcance**. Pertenece a `ft_market_data_manager`. |
| **Indicadores técnicos** | **Fuera de alcance**. Pertenece a `ft_vectorbt` / `ft_ffn`. |
| **Trading / Órdenes firmadas** | **Fuera de alcance**. Solo datos públicos. |
| **Símbolos apalancados / Opciones** | **Fuera de alcance MVP**. Solo Spot y USDⓈ-M Futures lineales. |
| **Reescritura de histórico ya descargado** | **Fuera de alcance**. Idempotencia = no tocar lo ya correcto. Re-procesar requiere `--force` (RFC futuro). |