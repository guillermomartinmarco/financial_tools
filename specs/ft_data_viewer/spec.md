# Spec — `ft_data_viewer` (MVP v1.0)

## Contexto e Historias de Usuario

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador cuantitativo, **quiero** explorar y validar los datasets OHLCV descargados **para que** pueda confirmar calidad, completitud y consistencia antes de usarlos en backtesting. |
| US-2 | Data Engineer | Como ingeniero de datos, **necesito** diagnosticar gaps, duplicados, outliers y estadísticas básicas **para que** pueda automatizar controles de calidad en pipelines de ingesta. |
| US-3 | Backtest Engineer | Como usuario de motores de backtesting, **quiero** filtrar y exportar subconjuntos de datos (por símbolo, timeframe, rango temporal) **para que** pueda preparar datasets específicos para cada experimento. |
| US-4 | DevOps / Operador | Como operador, **quiero** un CLI `ft_data_viewer` y endpoints API `GET /data/*` **para que** pueda auditar el estado de los datos desde terminal o desde la UI del `ft_api_service`. |

---

## Requisitos Funcionales (EARS)

### RF-1 — Listado de Datasets Disponibles (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `list` (CLI) / `GET /data/datasets` (API) que:
- Escanee recursivamente `FT_PARQUET_DIR` detectando todos los datasets particionados (exchange, market_type, symbol, timeframe). `market_type` ∈ {`spot`, `futures`, `equity`, `etf`, `index`, `fx`, `crypto`, `future`, `fund`, `other`} (Binance usa los dos primeros; Yahoo Finance el resto). Los árboles que no siguen el layout de velas (p. ej. `yfinance_events/`) se ignoran.
- Retorne para cada dataset: `exchange`, `market_type`, `symbol`, `timeframe`, `year_range` (min/max), `month_count`, `estimated_rows`, `size_bytes`, `last_modified`.
- Permita filtrar por `exchange`, `market_type`, `symbol`, `timeframe` (query params CLI/API).
- **SI** no hay datasets, **ENTONCES** retornar lista vacía (no error).

### RF-2 — Resumen Estadístico de Dataset (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `summary` (CLI) / `GET /data/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/summary` (API) que:
- Obtenga `row_count`, min/max y `null_count` de los metadatos del footer Parquet (sin leer filas) y calcule el resto (mean, distinct_count, gaps, duplicados, outliers) con una única agregación lazy proyectada solo sobre las columnas necesarias; nunca cargue el dataset completo en memoria.
- Retorne: `row_count`, `date_range` (min/max open_time_ms), `columns` con stats por columna (min, max, mean, null_count, distinct_count para numéricas).
- Detecte y reporte: `gaps_detected` (intervalos temporales faltantes), `duplicate_timestamps` (conteo), `outlier_count` (precios > 3 std devs de media).
- **SI** el dataset no existe, **ENTONCES** error 404 / CLI exit 1.

### RF-3 — Inspección de Muestras (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `head` / `tail` / `sample` (CLI) / `GET /data/datasets/{...}/rows` (API) que:
- `head N`: primeras N filas ordenadas por `open_time_ms` ASC.
- `tail N`: últimas N filas ordenadas por `open_time_ms` DESC.
- `sample N`: N filas aleatorias (reservoir sampling para datasets grandes).
- Permita proyección de columnas (`--cols open,high,low,close,volume`).
- Retorne tabla formateada (CLI: Rich table; API: JSON array).

### RF-4 — Detección de Gaps Temporales (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `gaps` (CLI) / `GET /data/datasets/{...}/gaps` (API) que:
- Detecte intervalos faltantes comparando timestamps consecutivos vs `timeframe` esperado (usar `TF_MS_MAP`).
- Retorne lista de gaps: `gap_start_ms`, `gap_end_ms`, `missing_candles_estimated`, `gap_duration_human`.
- Permita umbral mínimo (`--min-gap-candles`, default 1) para filtrar ruido.
- **SI** no hay gaps, **ENTONCES** retornar lista vacía.

### RF-5 — Detección de Duplicados y Anomalías (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `anomalies` (CLI) / `GET /data/datasets/{...}/anomalies` (API) que:
- Detecte `open_time_ms` duplicados (conteo y ejemplos).
- Detecte valores nulos en columnas críticas (`open, high, low, close, volume`).
- Detecte outliers de precio (Z-score > 3 o IQR method) en `open, high, low, close`.
- Detecte volúmenes anómalos (volumen = 0 o > 3 std devs).
- Retorne reporte estructurado por tipo de anomalía.

### RF-6 — Exportación de Subconjuntos (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `export` (CLI) / `POST /data/datasets/{...}/export` (API) que:
- Permita filtrar por rango temporal (`--start-ms`, `--end-ms`), símbolos, timeframes.
- Exporte a: Parquet (default), CSV, JSON Lines.
- Escriba en directorio especificado (`--output-dir`) o stdout (`-`).
- **SI** el rango solicitado no tiene datos, **ENTONCES** error 400 / CLI exit 3 con mensaje claro (exit 2 queda reservado para argumentos inválidos, ver T5.4).

### RF-7 — Comparación de Datasets (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `compare` (CLI) / `POST /data/compare` (API) que:
- Compare dos datasets (mismo symbol/timeframe, distinto exchange o market_type, o mismo dataset en distinto rango).
- Retorne: `row_count_diff`, `date_range_overlap`, `price_divergence_max` (max abs diff en close), `volume_divergence_pct`, `gaps_only_in_A`, `gaps_only_in_B`.
- Útil para validar consistencia entre Public Data vs REST, o Spot vs Futures.

### RF-8 — Logging Dual y Métricas (Ubícuo)
**EL SISTEMA DEBE** usar `structlog` configurado según modo (Art. 9 Constitución):
- **CLI**: Rich tables, progress bars para escaneos grandes, output JSON opcional (`--format json`).
- **API**: JSON estructurado con `task_id`, `component="ft_data_viewer"`, `dataset_id`, `duration_ms`. Como los endpoints del viewer son síncronos y no crean tareas en `tasks.db`, el `task_id` es un identificador generado por petición.
- Métricas: `scan_duration_ms`, `rows_scanned`, `bytes_read`.

### RF-9 — Configuración vía `FT_` Settings (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings` (prefijo `FT_`):
- `FT_PARQUET_DIR` (ya existe en ft_setup/ft_market_data_binance).
- `FT_DATA_VIEWER_SAMPLE_MAX_ROWS` (default 10000, para limitar sampling en datasets grandes).
- `FT_DATA_VIEWER_GAP_THRESHOLD_MS` (default 0, umbral para reportar gaps).

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Modificación de datos** (llenar gaps, corregir outliers, deduplicar) | **Fuera de alcance MVP**. Solo lectura y diagnóstico. RFC futuro para `ft_data_cleaner`. |
| **Visualización gráfica** (charts, candlestick plots) | **Fuera de alcance MVP**. Solo tablas y estadísticas. Exportar a CSV/Parquet para herramientas externas. |
| **Soporte para formatos no-Parquet** (CSV, HDF5, Zarr, DuckDB) | **Fuera de alcance MVP**. Solo Parquet particionado Hive. |
| **Aggregación/resampling de timeframes** (1m → 5m, 1h → 4h) | **Fuera de alcance MVP**. Pertenece a `ft_data_transformer` o motores de backtesting. |
| **Join con datos fundamentales/on-chain** | **Fuera de alcance**. Solo OHLCV de market data. |
| **Streaming/real-time data viewing** | **Fuera de alcance MVP**. Solo datos históricos persistidos. |
| **Permisos/ACL por dataset** | **Fuera de alcance MVP**. Asumimos entorno de confianza. |