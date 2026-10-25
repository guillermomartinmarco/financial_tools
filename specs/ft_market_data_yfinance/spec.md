# Spec — `ft_market_data_yfinance` (MVP v1.0)

## Contexto e Historias de Usuario

Yahoo Finance cubre lo que Binance no: acciones, ETFs, índices, forex, futuros y cripto cotizada contra fiat.
No tiene API oficial ni descarga masiva (no hay equivalente a *Public Data*), los mercados cierran
(fines de semana, feriados, horario de rueda) y el historial intradía está limitado. La herramienta replica
la filosofía de `ft_market_data_binance` (plan determinístico, descarga resiliente, normalización canónica,
Parquet Hive sin pérdida, tarea persistida con *polling*, CLI ≡ API) adaptada a esas diferencias.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** descargar OHLCV de acciones, ETFs, índices, forex, futuros y cripto desde Yahoo Finance para varios símbolos y timeframes **para que** pueda backtestear estrategias multi-activo con la misma suite que uso para Binance. |
| US-2 | Backtest Engineer | Como usuario del backtester, **exijo** precios **crudos** (lo que realmente cotizó) más `adj_close` y una tabla separada de dividendos/splits **para que** pueda calcular retornos totales o ajustes sin depender de que Yahoo recalcule el histórico. |
| US-3 | Data Engineer | Como ingeniero de datos, **necesito** que las descargas sean **incrementales, idempotentes y sin pérdida** y que los cierres de mercado **no** se traten como huecos **para que** re-ejecutar una descarga solo traiga lo nuevo. |
| US-4 | Operador | Como operador, **quiero** `ft_market_data_yfinance download` y `POST /market-data/yfinance/download` con el mismo contrato de polling que binance **para que** pueda lanzar descargas desde la terminal o desde `ft_serve`. |

---

## Requisitos Funcionales (EARS)

### RF-1 — Pipeline de Descarga Determinista (Ubícuo)
**EL SISTEMA DEBE** ejecutar, para cada combinación (`symbol`, `timeframe`) de la petición:
1. **Validar Petición**: `start_ms` < `now_ms`, `end_ms` ≥ `start_ms` (`end_ms` futuro se recorta a `now_ms`), timeframes dentro de RF-2. **SI** inválido, **ENTONCES** rechazo inmediato (exit 2 / HTTP 400) con detalle.
2. **Inventario Local**: leer las particiones existentes del símbolo bajo `{FT_PARQUET_DIR}/yfinance/market_type=*/symbol={SYMBOL}/timeframe={TF}/` (cualquier `market_type`) y obtener los intervalos cubiertos.
3. **Calcular Gaps**: rango pedido menos intervalos cubiertos; un intervalo cubre desde su primera vela hasta la última vela + 1 timeframe (RF-6).
4. **Aplicar Límites de Yahoo** (RF-3): la parte de cada gap fuera del historial disponible va a `gaps_unfillable` con motivo `beyond_yahoo_lookback`; el resto se divide en ventanas no mayores al máximo por pedido.
5. **Persistir el Plan** en `<raíz del proyecto>/share/plans/download_plan_{task_id}.json`.
6. **Descargar** cada ventana con concurrencia acotada (`FT_DOWNLOAD_MAX_CONCURRENT`, flag `--max-concurrent` / query `max_concurrent`) y rate-limit (RF-7).
7. **Normalizar** (RF-4), **escribir** velas y eventos (RF-5) y **cerrar la tarea** (RF-8).

### RF-2 — Timeframes Soportados (Ubícuo)
**EL SISTEMA DEBE** aceptar solo los timeframes canónicos del proyecto y traducirlos al intervalo de Yahoo:

| Canónico (disco y API) | Intervalo Yahoo |
|---|---|
| `1m` | `1m` |
| `5m` | `5m` |
| `15m` | `15m` |
| `30m` | `30m` |
| `1h` | `1h` |
| `1d` | `1d` |
| `1w` | `1wk` |
| `1M` | `1mo` |

**SI** se pide otro timeframe, **ENTONCES** exit 2 / HTTP 400. En disco se usa siempre el nombre canónico (compatible con `TF_MS_MAP` y con `ft_data_viewer`).

### RF-3 — Límites de Historial de Yahoo (Ubícuo)
**EL SISTEMA DEBE** respetar los límites medidos el 2026-10-25 con yfinance 1.7.0:

| Timeframe | Historial disponible (desde `now`) | Ventana máxima por pedido |
|---|---|---|
| `1m` | 30 días | 8 días |
| `5m`, `15m`, `30m` | 60 días | 60 días |
| `1h` | 730 días | 730 días |
| `1d`, `1w`, `1M` | sin límite | sin límite (un solo pedido) |

Los límites viven en una tabla de constantes (un único lugar) para poder ajustarlos si Yahoo los cambia.
**SI** un gap cae fuera del historial disponible, **ENTONCES** se registra en `gaps_unfillable` y la tarea **no** falla por ello.

### RF-4 — Normalización Canónica (Ubícuo)
**EL SISTEMA DEBE** pedir a Yahoo precios **sin ajustar** con acciones corporativas (`auto_adjust=False`, `actions=True`, excepciones visibles) y transformar cada lote **antes de persistir** a:

| Columna | Tipo | Origen |
|---|---|---|
| `symbol` | str | símbolo pedido, tal cual (p. ej. `AAPL`, `^GSPC`, `EURUSD=X`) |
| `timeframe` | str | canónico (RF-2) |
| `market_type` | str | `instrumentType` de Yahoo: EQUITY→`equity`, ETF→`etf`, INDEX→`index`, CURRENCY→`fx`, CRYPTOCURRENCY→`crypto`, FUTURE→`future`, MUTUALFUND→`fund` |
| `open_time_ms` | int64 | índice de Yahoo (zona horaria de la bolsa) convertido a ms UTC |
| `open`, `high`, `low`, `close` | float32 | precios crudos |
| `adj_close` | float32 | `Adj Close` de Yahoo |
| `volume` | float32 | `Volume` |

- Filas con OHLC nulo se descartan; duplicados por `open_time_ms` se resuelven conservando la última; salida ordenada.
- **SI** Yahoo devuelve un `instrumentType` no listado, **ENTONCES** `market_type="other"` y se loguea un warning.
- Las velas diarias o mayores tienen `open_time_ms` = medianoche local de la bolsa expresada en UTC.

### RF-5 — Persistencia en Parquet Hive sin Pérdida (Ubícuo)
**EL SISTEMA DEBE** escribir:
- **Velas** en `{FT_PARQUET_DIR}/yfinance/market_type={X}/symbol={Y}/timeframe={Z}/year={YYYY}/month={MM}/data.parquet`, con el mismo contrato de idempotencia sin pérdida que `ft_market_data_binance` RF-4 (merge con lo existente, dedupe por `open_time_ms` prevaleciendo lo nuevo, orden, escritura atómica).
- **Eventos corporativos** en `{FT_PARQUET_DIR}/yfinance_events/symbol={Y}/events.parquet` con columnas `event_time_ms` (int64), `event_type` (`dividend` | `split` | `capital_gain`), `value` (float64). Merge sin pérdida con dedupe por (`event_time_ms`, `event_type`).
- **SI** un lote no trae eventos, **ENTONCES** no se crea ni modifica el archivo de eventos.

### RF-6 — Cierres de Mercado No Son Gaps (Ubícuo)
- Los intervalos sin velas por mercado cerrado (fines de semana, feriados, fuera de rueda) **NO DEBEN** reportarse como gaps ni como errores.
- **SI** Yahoo devuelve una respuesta vacía para una ventana dentro del historial disponible, **ENTONCES** se considera "sin operaciones" y la ventana se da por procesada.
- El inventario considera cubierto todo el tramo entre la primera y la última vela de cada partición, así las re-ejecuciones solo piden lo posterior a la última vela.

### RF-7 — Rate-Limit y Reintentos (Ubícuo)
**EL SISTEMA DEBE** limitar los pedidos con un Token Bucket (`FT_YFINANCE_RATE_CAPACITY`, default 5; `FT_YFINANCE_RATE_REFILL_RATE`, default 1.0 pedido/s) y reintentar con backoff exponencial + jitter (`FT_YFINANCE_BASE_BACKOFF_MS`, default 2000) hasta `FT_YFINANCE_MAX_RETRIES` reintentos adicionales (default 3) **solo** ante rate-limit de Yahoo (HTTP 429) o errores de red/timeout (`FT_YFINANCE_TIMEOUT_S`, default 30). Cada reintento se loguea.

### RF-8 — Registro de Progreso en SQLTaskManager (Estado-Driven)
- Estados: `PENDING` → `PLANNING` → `DOWNLOADING` → `NORMALIZING` → `WRITING_PARQUET` → `COMPLETED` / `FAILED`; las fases solo avanzan.
- La fila de la tarea se crea al despachar (antes de planificar), con `task_id` UUID v7.
- `FAILED` **solo** si algún pedido agota los reintentos (RF-7); los datos ya escritos persisten. Símbolos inexistentes y gaps fuera de historial **no** causan `FAILED`.
- Errores por item se acumulan en `errors` del progreso y del resultado.

### RF-9 — Interfaces CLI y API Simétricas (Ubícuo)
- **CLI** `ft_market_data_yfinance`: `download` (con `--dry-run`), `plan`, `status`, `result` — `status`/`plan`/`result` imprimen el mismo JSON que la API. Flags globales `-v/-q/--log-format`. Exit codes: 0 OK, 1 no encontrado/genérico, 2 validación, 3 tarea `FAILED`, 4 error de IO.
- **API** (prefijo `/market-data/yfinance`): `POST /download` → `202` con `task_id`, `poll_url`, `plan_url`, `result_url`; `GET /status/{task_id}`, `GET /plan/{task_id}`, `GET /result/{task_id}` (`404` inexistente, `409` aún no terminado). El trabajo pesado nunca bloquea el event loop (Art. 3).

### RF-10 — Manejo de Errores Específicos (Evento-Driven)
| Situación | Comportamiento |
|---|---|
| Símbolo inexistente / sin datos (`YFPricesMissingError` "No data found") | Warning, se omite el símbolo, se registra en `errors`, la tarea sigue |
| Ventana fuera del historial disponible | `gaps_unfillable` (motivo `beyond_yahoo_lookback`), sin pedido a Yahoo |
| HTTP 429 / red / timeout | Reintentos RF-7; agotados → `FAILED` con detalle |
| `instrumentType` desconocido | `market_type="other"` + warning |

### RF-11 — Configuración vía `FT_` Settings (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings` (prefijo `FT_`): `FT_YFINANCE_RATE_CAPACITY`, `FT_YFINANCE_RATE_REFILL_RATE`, `FT_YFINANCE_MAX_RETRIES`, `FT_YFINANCE_BASE_BACKOFF_MS`, `FT_YFINANCE_TIMEOUT_S`, además de `FT_PARQUET_DIR`, `FT_DOWNLOAD_MAX_CONCURRENT` y `FT_TASKS_DB_PATH`. Todas registradas en `env_list.conf` con `source_tool="ft_market_data_yfinance"`. Yahoo no requiere API key.

---

## Casos Límite y Fuera de Alcance (No-Go)

- **Fuera de alcance**: datos fundamentales, opciones, noticias, pre/post-market (`prepost`), streaming en vivo, ajuste de precios al leer (lo hace el consumidor con `adj_close` o la tabla de eventos).
- **Intervalos no canónicos** de Yahoo (`2m`, `90m`, `5d`, `3mo`): no soportados.
- **Reparación de precios** de yfinance (`repair=True`): no se usa en el MVP (requiere SciPy y altera datos crudos).
- **Cambios de símbolo / deslistados**: se tratan como símbolo sin datos (RF-10).
