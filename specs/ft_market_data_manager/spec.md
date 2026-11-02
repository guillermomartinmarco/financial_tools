# Spec — `ft_market_data_manager` (MVP v1.0)

## Contexto e Historias de Usuario

`ft_market_data_manager` es la herramienta que **modifica** el almacén Parquet Hive de `FT_PARQUET_DIR`. Complementa a `ft_data_viewer`, que solo diagnostica (gaps, duplicados, anomalías) y tiene prohibido escribir: el viewer detecta el problema y el manager lo corrige. Además **importa** datos externos (Parquet, CSV, JSON Lines) al almacén canónico: es la operación inversa del `export` de `ft_data_viewer` (RF-6) y existe para reparar datos de forma masiva. Consolidar un rango en un único archivo (Parquet/CSV/JSONL) **ya lo hace** `ft_data_viewer export`; el manager no lo duplica.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Data Engineer | Como ingeniero de datos, **quiero** compactar y deduplicar las particiones de un dataset **para que** el almacén quede ordenado y sin filas repetidas tras muchas ingestas. |
| US-2 | Operador | Como operador, **necesito** borrar datasets o particiones antiguas con una vista previa y confirmación **para que** pueda liberar disco sin riesgo de perder datos por error. |
| US-3 | Data Engineer | Como ingeniero de datos, **quiero** importar velas corregidas desde Parquet, CSV o JSON Lines a un dataset concreto **para que** pueda reparar datos de forma masiva. |
| US-4 | Data Engineer | Como ingeniero de datos, **exijo** que la importación verifique el destino y el timestamp y nunca modifique los timestamps de filas existentes **para que** un archivo mal alineado o con zona horaria equivocada no corrompa un dataset. |
| US-5 | DevOps / Operador | Como operador, **quiero** la CLI `ft_market_data_manager` y endpoints `/market-data/manager/*` con polling **para que** pueda lanzar operaciones largas desde terminal o desde `ft_serve` sin bloquear. |

---

## Requisitos Funcionales (EARS)

Un **dataset** se identifica como en `ft_data_viewer`: `exchange` / `market_type` / `symbol` / `timeframe`. Una **partición** es un mes (`year=YYYY/month=MM/`). Las operaciones aceptan los mismos filtros de selección (`--exchange`, `--market-type`, `--symbol`, `--timeframe`), que admiten varios valores, y `--start-ms`/`--end-ms` para acotar meses. Los árboles que no siguen el layout de velas (p. ej. `yfinance_events/`) se ignoran.

### RF-1 — Compactación (Ubícuo)
**EL SISTEMA DEBE** proveer `compact` (CLI) / `POST /market-data/manager/compact` (API) que, por cada partición seleccionada:
- Consolide todos los archivos Parquet de la partición en un único `data.parquet`, ordenado por `open_time_ms` ascendente, **sin descartar ninguna fila** (la deduplicación es RF-2).
- Escriba de forma atómica (temporal + reemplazo) y elimine los archivos sobrantes solo después del reemplazo.
- Informe por partición: archivos antes/después, filas, bytes antes/después.

**SI** la partición ya es un único archivo ordenado, **ENTONCES** no se reescribe y se informa `unchanged` (idempotencia, Art. 12).

### RF-2 — Deduplicación (Ubícuo)
**EL SISTEMA DEBE** proveer `dedupe` (CLI) / `POST /market-data/manager/dedupe` (API) que elimine filas con `open_time_ms` repetido dentro de cada partición seleccionada:
- Entre filas con el mismo `open_time_ms` **prevalece la última** en el orden de lectura (archivos en orden lexicográfico, filas en orden de archivo). Es una regla determinista y se registra como supuesto: el almacén no guarda un criterio de frescura por fila.
- Informe por partición: duplicados eliminados y un ejemplo de `open_time_ms` afectado.
- El resultado queda ordenado y consolidado en un único `data.parquet` (incluye RF-1).

**SI** una partición no tiene duplicados, **ENTONCES** no se reescribe (`unchanged`).

### RF-3 — Retención y Borrado (Ubícuo)
**EL SISTEMA DEBE** proveer `prune` (CLI) / `POST /market-data/manager/prune` (API) que elimine particiones completas:
- Con `--before-ms T`: elimina las particiones cuyo mes termina antes de `T`. Una partición que contiene `T` **no se toca** (el borrado es por partición, nunca recorta filas).
- Con `--all`: elimina todas las particiones del dataset seleccionado. Exige al menos un filtro de dataset (`--exchange`/`--symbol`...): sin filtros es inválido.
- Tras borrar, elimina los directorios Hive que queden vacíos.

**SI** la operación no es `--dry-run` y no se indica `--force`, **ENTONCES** la CLI DEBE pedir confirmación explícita y, si se rechaza, abortar con exit 1 sin borrar nada. La API exige `confirm=true` en el payload; sin él responde 400.
**SI** no hay particiones que cumplan el criterio, **ENTONCES** éxito con lista vacía.

### RF-4 — Importación desde Parquet, CSV y JSON Lines (Ubícuo)
**EL SISTEMA DEBE** proveer `import` (CLI) / `POST /market-data/manager/import` (API) que cargue velas desde un archivo al almacén canónico:
- Formatos: Parquet, CSV y JSON Lines (`jsonl`), los mismos que exporta `ft_data_viewer` (RF-6). El formato se toma de `--format` o, si falta, de la extensión.
- **Destino explícito y obligatorio**: `--exchange`, `--market-type`, `--symbol`, `--timeframe`. Nunca se deduce del contenido del archivo. **SI** el dataset destino no existe, **ENTONCES** error 404 / CLI exit 1, salvo que se indique `--create` (evita crear datasets fantasma por un error de tipeo).
- **SI** el destino solo difiere en mayúsculas/minúsculas de un dataset existente (p. ej. `btcusdt` frente a `BTCUSDT`), **ENTONCES** error 400 / CLI exit 2 indicando el nombre exacto, también con `--create`: evita un `conflict` silencioso (sistemas insensibles a mayúsculas) o un dataset fantasma paralelo (sensibles).
- El archivo de origen **DEBE** estar fuera de `FT_PARQUET_DIR` (mismo criterio que el `output_dir` del viewer, ADR DV-01).
- Columnas: **DEBEN** ser exactamente las del esquema canónico del exchange destino, excluyendo `symbol`, `timeframe` y `market_type` (se derivan del destino). **SI** faltan columnas o sobran, **ENTONCES** error 422 / CLI exit 2 sin escribir nada. Los tipos se convierten a `float32`/`int64` con range-check (Art. 5.3, 13): precio > 0, volumen ≥ 0.
- Cada fila se asigna a su partición mensual según `open_time_ms`.
- **Dos fases**: primero se valida el archivo completo y se calcula el plan; solo si todo es válido se escribe. Un archivo inválido nunca deja el almacén a medias.
- Filas con valores inválidos (precio ≤ 0, nulos en columnas críticas): error de validación sin escribir; con `--skip-invalid` se importan las válidas y se informa el total rechazado y ejemplos.

### RF-5 — Salvaguardas de Timestamp y Destino (Ubícuo)
**EL SISTEMA DEBE**, en `import`:
- **Timestamp explícito**: la columna de tiempo es `open_time_ms` (`int64` ms UTC). Si el archivo usa otro nombre o unidad, **DEBE** indicarse `--ts-column NOMBRE` y `--ts-unit s|ms|us|ns`; no se adivina la unidad. Los timestamps como texto ISO-8601 no se aceptan (Art. 5.4).
- **Plausibilidad**: tras la conversión, todo `open_time_ms` **DEBE** estar entre 2000-01-01 UTC y `ahora + 1 día`. Fuera de ese rango, error 422 / exit 2 con una pista (p. ej. "los valores parecen estar en segundos; use `--ts-unit s`").
- **Alineación a la grilla del timeframe**: cada `open_time_ms` **DEBE** caer en el inicio de una vela del timeframe destino (múltiplo exacto de la duración para los fijos; `1d` a las 00:00 UTC; `1w` en lunes 00:00 UTC; `1M` en el día 1 a las 00:00 UTC). Una fila desalineada indica zona horaria equivocada o timeframe equivocado: error de validación sin escribir, informando la cantidad y un ejemplo (nunca se redondea ni se corrige en silencio).
- **Duplicados en el origen**: **SI** el archivo repite `open_time_ms`, **ENTONCES** error de validación con ejemplos (no se elige una fila en silencio).
- **Las filas existentes nunca cambian de timestamp**: la combinación con una partición existente solo añade filas nuevas o, si se pide, reemplaza los valores de una fila con el mismo `open_time_ms`. Ninguna fila existente se mueve ni se reescribe con otro `open_time_ms`.
- **Conflictos** (filas del archivo con un `open_time_ms` ya presente en el destino), `--on-conflict`:
  - `skip` (por defecto): conserva la fila existente y no importa la del archivo.
  - `fail`: aborta si hay al menos un conflicto.
  - `replace`: reemplaza los valores de esas filas. **DEBE** informar la divergencia máxima relativa de `close` entre ambas versiones; **SI** supera el 1 % (supuesto, indicio de timestamp desplazado), **ENTONCES** aborta salvo `--force`.
- **Vista previa obligatoria**: antes de escribir, **DEBE** mostrar el destino, el rango, filas nuevas, conflictos, particiones nuevas y particiones modificadas. En la CLI pide confirmación salvo `--force` o `--dry-run`; en la API, sin `confirm=true` ni `dry_run=true` responde 400.

### RF-6 — Seguridad de las Operaciones que Modifican (Ubícuo)
**EL SISTEMA DEBE**, en `compact`, `dedupe`, `prune` e `import`:
- Aceptar `--dry-run` / `dry_run=true`: calcula y devuelve el plan (particiones, filas, bytes afectados) **sin escribir ni borrar nada**.
- Verificar, inmediatamente antes de reemplazar o borrar una partición, que sus archivos no cambiaron desde que se leyeron (tamaño y `mtime`). **SI** cambiaron (p. ej. un descargador escribió en paralelo), **ENTONCES** omitir esa partición, marcarla `conflict` y seguir con las demás. Nunca se pisa una escritura ajena.
- Dejar cada partición en su versión anterior o en la nueva, nunca a medias (escritura atómica con `os.replace`); una re-ejecución completa lo que quedó pendiente.
- **SI** tras reemplazar `data.parquet` no se puede borrar un archivo sobrante (p. ej. abierto por otro proceso en Windows), **ENTONCES** informar la partición como `error` con el estado real en disco (archivos que quedaron) y recomendar `dedupe`, porque el sobrante duplica filas hasta limpiarlo; no se pierden filas.
- Procesar un dataset a la vez dentro de una operación; los errores de una partición no abortan las restantes y se acumulan en el resultado.

### RF-7 — Despacho Asíncrono y Polling (Ubícuo)
**EL SISTEMA DEBE** exponer bajo FastAPI (prefijo `/market-data/manager`, Art. 18):
- `POST /compact`, `/dedupe`, `/prune`, `/import`: validan, crean la tarea en `SQLTaskManager` (`share/tasks.db`, WAL) y la despachan en `BackgroundTasks`; responden `task_id` (UUID v7) y `poll_url` de inmediato.
- `GET /status/{task_id}`: estado y progreso (`PENDING` → `RUNNING` → `COMPLETED` / `FAILED`; campos `partitions_total`, `partitions_done`, `partitions_changed`, `partitions_conflict`, `rows_affected`, `bytes_before`, `bytes_after`, `errors_json`).
- `GET /result/{task_id}`: resumen final por partición. `FAILED` solo ante error de IO o excepción inesperada; las particiones en `conflict` o con error puntual no hacen fallar la tarea.
- **SI** `dry_run=true`, **ENTONCES** el resultado es el plan y no se modifican datos.

La CLI ejecuta la misma lógica de forma síncrona con barra de progreso Rich y acepta `--format table|json`.

### RF-8 — Logging Dual y Auditoría (Ubícuo)
**EL SISTEMA DEBE** usar `structlog` (Art. 9). Toda operación que modifica el almacén registra un evento de auditoría INFO con `action`, `task_id`, `dataset`, `partition`, `rows_before`, `rows_after`, `bytes_before`, `bytes_after`, `dry_run` y `result` (`changed` | `unchanged` | `conflict` | `error`). **CLI**: tablas Rich. **API**: JSON estructurado con `component="ft_market_data_manager"`. Métricas: `duration_ms`, `partitions_scanned`, `rows_processed`.

### RF-9 — Configuración vía `FT_` Settings (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings` (prefijo `FT_`) `FT_PARQUET_DIR` y `FT_TASKS_DB_PATH`. No introduce variables nuevas.

### RF-10 — Errores y Códigos de Salida (Evento-Driven)
| Condición | CLI exit | API |
|-----------|----------|-----|
| OK (incluye `--dry-run`, `unchanged` y partes en `conflict`) | 0 | 200 / 202 |
| Dataset destino o archivo de origen no encontrado | 1 | 404 |
| Argumentos inválidos, filtros ausentes en `prune --all`, columnas, timestamp o alineación inválidos en `import` | 2 | 400 / 422 |
| Conflicto con `--on-conflict fail`; divergencia > 1 % en `replace` sin `--force`; confirmación faltante | 3 | 409 / 400 |
| Error de IO (disco, permisos) | 4 | 500 |
| Confirmación rechazada por el usuario | 1 | — |

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Rellenar gaps** (descargar velas faltantes) | **Fuera de alcance MVP**. Requeriría invocar a otra `ft_*` (Art. 4). El viewer detecta el gap y el usuario lo repara con `ft_market_data_binance`/`ft_market_data_yfinance`. |
| **Corregir outliers / editar valores** | **Fuera de alcance**. El manager nunca altera el contenido de una fila. |
| **Recortar filas dentro de una partición** | **Fuera de alcance MVP**. El borrado es por partición completa. |
| **Resampling / agregación de timeframes** | **Fuera de alcance**. Pertenece a motores de backtesting o a una herramienta de transformación. |
| **SQLite** (exportar/importar) | **Fuera de alcance**. Decisión del usuario: Parquet, CSV y JSON Lines son suficientes. |
| **Otros formatos** (HDF5, Zarr, DuckDB) | **Fuera de alcance MVP**. |
| **Consolidar un rango en un único archivo** | **Ya cubierto** por `ft_data_viewer export` (RF-6). No se duplica. |
| **Servir datos de mercado a aplicaciones cliente** (`GET` por rango) | **Fuera de este manager**: es lectura; ver `ft_data_viewer`. |
| **Sobrescribir o reescribir timestamps de filas existentes** | **Prohibido**. `import` solo añade filas o reemplaza valores de un `open_time_ms` ya existente. |
| **Eventos corporativos** (`yfinance_events/`) | **Fuera de alcance MVP**. Solo velas OHLCV. |
| **Bloqueo cooperativo con los descargadores** | **Fuera de alcance MVP**. Se usa verificación optimista por partición (RF-6); los descargadores no se modifican. Se recomienda no ejecutarlos en paralelo con una operación que modifica el mismo dataset. |
| **Permisos/ACL, programación periódica de retención** | **Fuera de alcance MVP**. |
