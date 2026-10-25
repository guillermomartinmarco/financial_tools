# Spec — `ft_datasets` (MVP v1.0)

## Contexto e Historias de Usuario

Los descargadores (`ft_market_data_binance`, `ft_market_data_yfinance`) dejan OHLCV canónico en Parquet Hive y los
tests de la suite son, por diseño, sin red y deterministas. Falta una forma de **probar masivamente con datos
reales** que dé el mismo resultado en cualquier máquina y en CI. `ft_datasets` introduce las **recetas de datasets**:
un archivo versionado en el repositorio (`datasets/<nombre>.toml`) que fija exchange, `market_type`, símbolos,
timeframes y **rangos de tiempo cerrados y pasados**, acompañado de un *lockfile* (`datasets/<nombre>.lock.json`) con
el **hash de contenido canónico** esperado de cada dataset. La herramienta **materializa** una receta usando los
descargadores existentes (sin reimplementarlos), **verifica** los hashes y **bloquea** (`lock`) los hashes la primera
vez, siempre de forma explícita.

El hash se define sobre el **contenido canónico** (`open_time_ms` + columnas numéricas como patrones de bits
exactos), no sobre los bytes del archivo Parquet: los bytes dependen del escritor (compresión, *row groups*,
estadísticas, versión de Polars) y el contenido no.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** declarar en un archivo versionado qué datos reales usa una batería de pruebas **para que** cualquiera reproduzca exactamente los mismos datos en su máquina. |
| US-2 | Data Engineer | Como ingeniero de datos, **necesito** que la materialización reutilice los descargadores, sea idempotente y **no use la red** si los datos locales ya coinciden con el *lock* **para que** repetirla sea gratis. |
| US-3 | Mantenedor de CI | Como mantenedor, **quiero** una suite opt-in de datos reales en CI con los datasets cacheados por hash de receta **para que** la descarga ocurra una sola vez y la suite por defecto siga sin red. |
| US-4 | Backtest Engineer | Como consumidor, **exijo** que la verificación distinga "datos iguales", "datos ausentes" y "el proveedor re-expresó el histórico" (p. ej. un *split* de Yahoo) **para que** sepa si el fallo es mío, de la red o del proveedor. |
| US-5 | Operador | Como operador, **quiero** `ft_datasets materialize` y `POST /datasets/materialize/{name}` con el mismo contrato de *polling* que el resto de la suite **para que** pueda lanzarlo desde la terminal o desde `ft_serve`. |

---

## Requisitos Funcionales (EARS)

### RF-1 — Receta Versionada (Ubicuo)
**EL SISTEMA DEBE** leer recetas TOML desde `FT_DATASETS_RECIPES_DIR` (por defecto `<raíz>/datasets`) con el
contrato `DatasetRecipe` (plan §1.1) y validarlas con Pydantic (Art. 13):
- `schema_version = 1`, `name` igual al nombre del archivo (`^[a-z0-9][a-z0-9._-]{0,63}$`), `description` libre.
- Uno o más bloques `[[datasets]]` con `exchange` (`binance` | `yfinance`), `market_type` (obligatorio en ambos
  exchanges), `symbols` (1..100), `timeframes` (≥ 1), `start` y `end` como fechas `YYYY-MM-DD` interpretadas a las
  00:00 UTC, con `end` **exclusivo**.
- Cada bloque se expande al producto cartesiano `symbols × timeframes`; cada elemento es un **ítem** con clave
  `{exchange}/{market_type}/{symbol}/{timeframe}@{start}..{end}`. **SI** dos ítems tienen la misma clave, **ENTONCES**
  la receta es inválida.
- **SI** la receta es inválida, **ENTONCES** rechazo inmediato (exit 2 / HTTP 400) con el detalle de Pydantic.

### RF-2 — Solo Rangos Reproducibles (No deseado)
- **SI** `end` es posterior a `hoy − FT_DATASETS_SETTLE_DAYS` (por defecto 7 días), **ENTONCES** la receta se rechaza
  (exit 2 / HTTP 400): un rango abierto o reciente nunca es reproducible (velas en formación, revisiones tardías del
  proveedor, archivos mensuales de Binance Public Data aún no publicados).
- **SI** un bloque `yfinance` pide un timeframe con historial limitado por Yahoo (`1m`, `5m`, `15m`, `30m`, `1h`),
  **ENTONCES** la receta se rechaza: ese historial caduca a los 30–730 días y la receta dejaría de ser materializable.
  Solo se admiten `1d`, `1w`, `1M` para `yfinance`.
- Para `binance` se admiten los timeframes canónicos de `finances_core.schemas.market_data.Timeframe` y `market_type`
  `spot` | `futures`; para `yfinance`, los `market_type` de `DatasetIdentifier` salvo `other`.
- **EL SISTEMA NO DEBE** aceptar `now`, fechas relativas ni `end` ausente.

### RF-3 — Hash de Contenido Canónico `ftds-v1` (Ubicuo)
**EL SISTEMA DEBE** calcular, para cada ítem, un hash SHA-256 sobre el **contenido canónico** de las filas con
`open_time_ms ∈ [start_ms, end_ms)` (algoritmo exacto en plan §3.2):
- Filas ordenadas por `open_time_ms`; un `open_time_ms` duplicado o un dtype no canónico es un error de datos.
- **Columnas estrictas** (por exchange, orden fijo): `binance` = `open_time_ms`, `open`, `high`, `low`, `close`,
  `volume`, `quote_volume`, `trades_count`, `taker_buy_base_volume`, `taker_buy_quote_volume`; `yfinance` =
  `open_time_ms`, `open`, `high`, `low`, `close`, `volume`.
- **Columnas informativas**: `yfinance` = `adj_close` (Yahoo la re-expresa con cada dividendo nuevo; su cambio se
  reporta como aviso, nunca como fallo).
- Codificación: `int64` como 8 bytes *little-endian*; `float32` como sus 4 bytes IEEE 754 *little-endian*
  **exactos**, salvo NaN, que se canonicaliza a `0x7FC00000`; nulos codificados aparte (posiciones), nunca como
  valor.
- **EL SISTEMA DEBE** producir el mismo hash con independencia del escritor Parquet (compresión, tamaño de *row
  group*, estadísticas), del sistema operativo y de la CPU.
- Además del hash total, **DEBE** calcular un hash por columna y un hash por mes UTC (localización de deriva) y tres
  **sondas** (`close` de la primera, la central y la última fila) para clasificar la deriva (RF-6).

### RF-4 — Materialización con los Descargadores Existentes (Ubicuo)
**EL SISTEMA DEBE** materializar una receta así:
1. **Validar** la receta (RF-1, RF-2) y cargar el *lock* si existe.
2. **Pre-verificar** cada ítem contra el almacén `FT_DATASETS_DIR` (RF-5). Los ítems `OK` se omiten.
3. **Descargar** los ítems restantes, por bloque y en secuencia, invocando la **CLI pública** del descargador del
   exchange como subproceso (`ft_market_data_binance download` / `ft_market_data_yfinance download`) con
   `FT_PARQUET_DIR` = `FT_DATASETS_DIR`, solo para los símbolos × timeframes del bloque no verificados, rango
   `[start_ms, end_ms − 1]` en ms epoch, `--log-format json -q`, y un tiempo máximo `FT_DATASETS_DOWNLOAD_TIMEOUT_S`.
4. **Verificar** todos los ítems (RF-5) salvo `--no-verify`.
5. **Cerrar la tarea** (RF-8). La materialización **NUNCA** escribe el *lock*.
- **SI** todos los ítems están `OK` en la pre-verificación, **ENTONCES** no se lanza ningún subproceso ni se usa la red
  (Art. 12; un CI con caché caliente no descarga nada).
- **SI** se pasa `--refetch`, **ENTONCES** antes de descargar se borran del almacén `FT_DATASETS_DIR` (y solo de él)
  las particiones mensuales que solapan los ítems en `MISMATCH`, para que el descargador las vuelva a pedir.

### RF-5 — Verificación (Ubicuo)
**EL SISTEMA DEBE** verificar una receta contra su *lock* leyendo solo las particiones mensuales que solapan cada ítem
y devolver un `VerifyReport` con un estado por ítem:

| Estado | Condición |
|---|---|
| `OK` | hash estricto igual al del *lock* |
| `OK_WITH_WARNINGS` | hash estricto igual; difiere alguna columna informativa (`adj_close`) |
| `MISSING` | ninguna fila en el rango |
| `MISMATCH` | hash estricto distinto (con clasificación RF-6) |
| `UNLOCKED` | el *lock* no existe o no contiene el ítem |

- Estado global: `RECIPE_CHANGED` **SI** el hash canónico de la receta difiere del registrado en el *lock* (todos los
  ítems se evalúan igualmente); `MISMATCH` **SI** algún ítem es `MISSING` o `MISMATCH`; `UNLOCKED` **SI** no hay
  fallos pero algún ítem está `UNLOCKED`; `OK` en otro caso.
- La verificación es local (sin red) y **DEBE** ser apta para CI: `benchmark-v1` (~270 k filas) en ≤ 5 s.

### RF-6 — Clasificación de la Deriva (Evento-Driven)
**SI** un ítem es `MISMATCH`, **ENTONCES** el informe **DEBE** incluir:
- `drift`: `ROWS_CHANGED` (cambian el número de filas, `first/last_open_time_ms` o el hash de `open_time_ms`),
  `SCALE_RESTATED` (mismos `open_time_ms`, cambian todas las columnas de precio y las tres sondas guardan el mismo
  cociente actual/bloqueado con tolerancia relativa 1e-3; se reporta el factor, p. ej. un *split* 4:1 de Yahoo) o
  `VALUES_CHANGED` (resto de casos).
- Las columnas cuyo hash difiere y los meses (`YYYY-MM`) cuyo hash difiere.
- Un texto de acción: re-materializar con `--refetch` si se sospecha corrupción local; re-bloquear con
  `lock --update` (en un PR revisado) si el proveedor re-expresó el histórico.

### RF-7 — Bloqueo Explícito (Evento-Driven)
- **CUANDO** se ejecuta `lock NAME` y no existe *lock*, **EL SISTEMA DEBE** calcular los hashes de todos los ítems
  desde el almacén local y escribir `datasets/<nombre>.lock.json` de forma atómica.
- **SI** algún ítem está `MISSING`, **ENTONCES** no se escribe nada (exit 5 / HTTP 409): nunca se bloquea un dataset
  vacío.
- **SI** el *lock* existe y coincide, **ENTONCES** no se reescribe (resultado `UNCHANGED`, idempotente).
- **SI** el *lock* existe y difiere (o la receta cambió), **ENTONCES** se rechaza (exit 5 / HTTP 409) listando las
  diferencias, salvo con `--update` / `update=true`, que reescribe el *lock*, devuelve el diff y lo loguea como
  `warning`.
- El *lock* registra además, de forma informativa, `missing_candles` (velas esperadas por calendario 24/7 menos filas,
  solo `binance`) y las versiones de `finances-downloader`, `polars` y `yfinance` usadas.
- **EL SISTEMA NO DEBE** escribir el *lock* de forma implícita (ni `materialize`, ni `verify`, ni CI).

### RF-8 — Registro de Progreso en SQLTaskManager (Estado-Driven)
- `materialize` es una tarea (Art. 2, Art. 18) con `task_id` UUID v7 creada al despachar.
- Fases: `PENDING` → `VALIDATING` → `PRECHECKING` → `DOWNLOADING` → `VERIFYING` → `COMPLETED` / `FAILED`; solo
  avanzan.
- `FAILED` **solo** si un subproceso de descarga termina con código distinto de 0, supera el tiempo máximo o no puede
  lanzarse; los datos ya escritos persisten. Un `MISMATCH` **no** es `FAILED`: la tarea termina `COMPLETED` y el
  resultado lleva `outcome = MISMATCH`.
- El plan (ítems, bloques y comandos exactos) se persiste en `<raíz>/share/plans/datasets_plan_{task_id}.json`.
- Por bloque se registran código de salida, duración, las últimas 50 líneas de `stderr` y, si aparece en los logs
  JSON del descargador, su `task_id` (para `ft_market_data_* result`).

### RF-9 — Interfaces CLI y API Simétricas (Ubicuo)
- **CLI** `ft_datasets`: `list`, `show NAME`, `materialize NAME` (`--no-verify`, `--refetch`, `--dry-run`),
  `verify NAME` (`--format json|markdown`), `lock NAME` (`--update`), `status`, `plan`, `result`. Flags globales
  `-v/-q/--log-format`. Exit codes: 0 OK, 1 no encontrado/genérico, 2 validación, 3 tarea `FAILED`, 4 IO,
  5 verificación fallida (`MISMATCH`/`MISSING`/`RECIPE_CHANGED`, o `lock` rechazado), 6 sin bloquear (`UNLOCKED`).
- **API** (prefijo `/datasets`): `GET /recipes`, `GET /recipes/{name}`, `POST /materialize/{name}` → `202`
  `DispatchResponse`; `GET /status|/plan|/result/{task_id}` (`404`, `409` si no terminó); `POST /verify/{name}` →
  `VerifyReport`; `POST /lock/{name}` → `LockResult` (`409` si se rechaza). El trabajo pesado nunca bloquea el event
  loop (Art. 3).
- Los JSON de CLI y API son idénticos para el mismo modelo (test de contrato).
- `name` en la API se valida con la expresión de RF-1 y se resuelve **solo** dentro de `FT_DATASETS_RECIPES_DIR`
  (sin rutas arbitrarias).

### RF-10 — Suite Opt-in de Datos Reales (Ubicuo)
- **EL SISTEMA DEBE** ofrecer `finances_core.datasets.testing.require_dataset(recipe, key)`, que verifica el ítem y
  devuelve un `LazyFrame`; **SI** el ítem no está `OK`, **ENTONCES** el test **falla** (nunca se omite en silencio).
- Los tests de datos reales llevan el marcador `real_data`, excluido por defecto (`-m 'not real_data'`) en todos los
  paquetes: la suite por defecto sigue **sin red y determinista**.
- **CI**: workflow separado (`.github/workflows/real-data.yml`) en `ubuntu-latest`, disparado manualmente, por
  calendario semanal o por la etiqueta `real-data` de un PR; caché de `FT_DATASETS_DIR` con clave derivada del hash
  de la receta y su *lock*; la caché solo se guarda tras una verificación `OK` (plan §3.8).

### RF-11 — Configuración vía `FT_` Settings (Ubicuo)
**EL SISTEMA DEBE** leer de `FinancesSettings`: `FT_DATASETS_RECIPES_DIR` (`./datasets`), `FT_DATASETS_DIR`
(`./data/datasets`), `FT_DATASETS_DOWNLOAD_TIMEOUT_S` (3600), `FT_DATASETS_SETTLE_DAYS` (7), además de
`FT_TASKS_DB_PATH`. Todas registradas en `env_list.conf` con `source_tool="ft_datasets"`.
**SI** `FT_DATASETS_DIR` está dentro de `FT_PARQUET_DIR` o lo contiene, **ENTONCES** error de configuración al usarlo:
el almacén de recetas es una caché reconstruible y no se mezcla con las descargas del usuario.

### RF-12 — Manejo de Errores Específicos (Evento-Driven)
| Situación | Comportamiento |
|---|---|
| Receta inexistente | exit 1 / HTTP 404 |
| Receta inválida o rango no reproducible | exit 2 / HTTP 400 con detalle |
| CLI del descargador no encontrada | `FAILED` con el nombre del ejecutable buscado |
| Subproceso con exit ≠ 0 o *timeout* | `FAILED`; se conserva `stderr` (últimas 50 líneas) en el resultado |
| Descargador `COMPLETED` pero sin filas para un ítem (símbolo inexistente) | La tarea sigue; el ítem queda `MISSING` en la verificación |
| *Lock* corrupto o de otro algoritmo (`hash_algorithm ≠ ftds-v1`) | exit 2 / HTTP 400; nunca se reinterpreta |
| Error de IO al leer/escribir el almacén o el *lock* | exit 4 / HTTP 500 |

---

## Casos Límite y Fuera de Alcance (No-Go)

- **Servidor MCP**: fuera de alcance (decisión del usuario).
- **Rangos abiertos o relativos** (`now`, "últimos 30 días"): no reproducibles, prohibidos (RF-2).
- **Intradía de Yahoo**: prohibido en recetas (RF-2).
- **Tabla de eventos de Yahoo** (`yfinance_events/`): no forma parte del hash en v1.
- **Otras fuentes** (ticks, *order books*, *funding*): fuera de alcance; solo OHLCV de los dos descargadores.
- **Actualización automática del *lock*** (en CI o al materializar): prohibida (RF-7).
- **Distribución de datos** (subir datasets a un bucket o *release*): fuera de alcance; la reproducibilidad descansa en
  re-descargar del proveedor y verificar el hash. Si un proveedor re-expresa el histórico, el *lock* se actualiza con
  un PR revisado.
- **Ejecución en Windows del workflow de datos reales**: no; la portabilidad del hash entre SO se prueba en la suite
  por defecto (matriz Windows + Ubuntu) con vectores dorados (plan §3.2).
