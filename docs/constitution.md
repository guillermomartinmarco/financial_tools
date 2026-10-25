# Constitución Técnica de la Suite `financial_tools`

**Versión:** 1.0.0  
**Estado:** Vigente  
**Alcance:** Obligatorio para todos los contribuyentes, agentes de IA y pipelines de CI/CD.  
**Ubicación canónica:** `docs/constitution.md`

---

## Preámbulo

Esta constitución establece los principios de ingeniería innegociables que gobiernan el ciclo de vida de la suite `financial_tools`. Su propósito es garantizar la coherencia arquitectónica, la **normalización universal de datos de mercado**, la integridad numérica controlada, la observabilidad operativa y la escalabilidad sostenida. Todo código, configuración o proceso que viole estos artículos será rechazado en revisión.

---

## Artículo 1 — Dualidad Consola/Servicio (Mandatoria)

Todo componente funcional expuesto al exterior **DEBE** implementar una interfaz de línea de comandos (CLI) mediante **Typer** y un endpoint asíncrono bajo **FastAPI** de forma simétrica. La lógica de negocio reside exclusivamente en capas de servicio compartidas; CLI y API son *adaptadores* delgados. Queda prohibida la duplicación de lógica entre ambos adaptadores.

## Artículo 2 — Persistencia Inmune (SQLite WAL)

El estado de procesamiento pesado (tareas, progresos, resultados intermedios) **DEBE** persistirse en **SQLite** con modo **WAL (Write-Ahead Logging)** habilitado de manera obligatoria. El gestor canónico es `SQLTaskManager` (módulo `finances_core.tasks`), y la base de datos única vive en `<raíz del proyecto>/share/tasks.db`, resuelta de forma absoluta (nunca relativa al directorio de trabajo actual). El modo WAL garantiza lecturas concurrentes *polling* ultra-rápidas sin bloquear escrituras.

## Artículo 3 — Operaciones No Bloqueantes (Event Loop Protection)

El bucle de eventos de FastAPI **JAMÁS** se bloqueará por operaciones de I/O intensivo, cómputo matemático pesado o esperas activas. Toda tarea de larga duración **DEBE** despacharse a `BackgroundTasks`, hilos dedicados (`run_in_threadpool`) o subprocesos aislados. El endpoint de ejecución responderá inmediatamente (ver Art. 18).

## Artículo 4 — Aislamiento de Módulos (Cross-Import Ban)

Queda **PROHIBIDO** realizar *imports* directos entre utilidades hermanas `ft_*`, sin importar en qué paquete de `packages/` residan (p. ej. `finances_downloader.ft_data_viewer` no puede importar de `finances_downloader.ft_market_data_binance`). El único puente legítimo para compartir código, contratos, modelos o utilidades es el paquete interno **`packages/finances-core`**. La única excepción es la raíz de composición **`packages/finances-api`** (no es una utilidad `ft_*`): puede importar el `router` público de cada utilidad para montarlos en una sola app, y ninguna utilidad puede importar de ella. Cualquier dependencia cruzada detectada en CI provocará fallo de *merge*.

## Artículo 5 — Representación Numérica Unificada (float32 / int64)

**5.1 Tipos canónicos**  
Todos los datos de mercado normalizados **DEBEN** usar exclusivamente:
- **`float32`** (IEEE 754 single-precision) para: *precios* (open, high, low, close, vwap, bid, ask, mid), *volúmenes* (base, quote, notional), *indicadores* (RSI, MACD, ATR, etc.), *métricas derivadas* (returns, volatilidad, correlaciones).
- **`int64`** (milisegundos desde epoch UTC) para: *timestamps* de apertura/cierre de vela, *event times* de ticks, *exchange timestamps*, *receive timestamps*.

**5.2 Normalización Universal Obligatoria**  
**TODOS** los datos de entrada (OHLCV, *ticks*, *order books* L2/L3, *funding rates*, *open interest*, *option chains*, *on-chain metrics*, etc.) provenientes de **CUALQUIER** exchange (CEX/DEX), *data vendor*, *broker*, *blockchain indexer* o *feed* propietario **DEBEN** ser transformados **inmediatamente tras la ingesta** al esquema canónico unificado definido en `packages/finances-core/schemas/market_data.py` (o equivalente Pydantic/Polars).  
El esquema canónico define: nombres de columnas, unidades, zona horaria (UTC), convención de *timestamp* (inicio de vela, milisegundos), y tipos (`float32`/`int64`). No existe "formato nativo del proveedor" en capas de procesamiento; solo existe el **formato normalizado**.

**5.3 Conversión y Validación**  
La conversión a `float32`/`int64` **DEBE** ser validada por Pydantic (Art. 13) con reglas de *range-check* (ej. precio > 0, volumen ≥ 0, timestamp monótono creciente). Pérdida de precisión por *downcast* a `float32` es **aceptada y documentada** como *trade-off* deliberado para rendimiento GPU/CPU y homogeneidad de tensores; cualquier caso que requiera mayor precisión (ej. *high-frequency arbitrage*, *smart contract settlement*) debe ser explícitamente justificado en RFC y aislado en módulo dedicado.

**5.4 Prohibiciones**  
- Queda **PROHIBIDO** el uso de `float64`/`double`, `Decimal`, `Decimal128` o *wrappers* monetarios (`Money`, `Currency`) en *data paths* de procesamiento masivo, *feature engineering*, inferencia ML, o kernels CUDA.
- Queda **PROHIBIDO** almacenar o transmitir *timestamps* como strings ISO-8601, nanosegundos, o *timezone-aware* objects en capas de procesamiento. Solo `int64` ms UTC.

## Artículo 6 — Manejo de Errores bajo Notación EARS

Todo flujo alterno, condición de fallo, validación rechazada o excepción de negocio **DEBE** documentarse en los requisitos funcionales (`spec.md`) usando notación **EARS** formal (*UBICUO*, *EVENTO-DRIVEN*, *ESTADO-DRIVEN*, *OPCIONAL*, *NO DESEADO*). El código de manejo de errores debe mapearse 1:1 a dichos requisitos.

## Artículo 7 — Estrategia Test-First (Definición de Hecho)

Ninguna tarea (`tasks.md`) se marcará como completada (**Done**) sin que exista su **suite de tests unitarios** correspondiente cubriendo: camino feliz, bordes, y al menos un caso de fallo documentado en EARS (Art. 6). Cobertura mínima objetivo: **90%** sobre la lógica de servicio (no adaptadores).

## Artículo 8 — Polars sobre Pandas (Procesamiento Masivo)

Para el procesamiento masivo de series temporales financieras (OHLCV, *ticks*, *order books*, indicadores) **Polars** es la biblioteca por defecto. Su modelo de ejecución *lazy*, ausencia de GIL en expresiones y API *DataFrame* nativa superan a Pandas en rendimiento y seguridad de hilos. Pandas solo se admite en capa de visualización final (*plotting*) si la dependencia lo exige.  
**Los *DataFrames* de Polars en toda la suite DEBEN usar dtypes `Float32` e `Int64` (ms) conforme al Art. 5.**

## Artículo 9 — Estructura de Logs (Dual Mode: Consola + JSON)

**9.1 Modo Servicio (FastAPI / Background / Workers)**  
**TODO** log emitido **DEBE** ser estructurado en **JSON** a través de **`structlog`**, inyectando contexto obligatorio: `task_id`, `symbol`, `timeframe`, `trace_id`, `span_id`, `component`. Niveles: `debug`, `info`, `warning`, `error`, `critical`. Salida: *stdout* (capturado por *log shipper*: Loki, Datadog, etc.).

**9.2 Modo Consola (CLI / Typer)**  
Los comandos CLI **DEBEN** emitir logs **legibles y agradables** en *stderr* usando **`rich.logging.RichHandler`** (colores, *tracebacks* bonitos, barras de progreso, *spinners*, tablas). El nivel por defecto es `INFO`; `--verbose` eleva a `DEBUG`; `--quiet` reduce a `WARNING`. **No se emite JSON en CLI** salvo flag explícito `--log-format json` (para *scripting*).

**9.3 Implementación**  
`structlog` se configura una sola vez en *entrypoint* (CLI o API) seleccionando el *processor pipeline* según modo. Los *loggers* de librería (`structlog.get_logger(__name__)`) son agnósticos al formato de salida.

## Artículo 10 — Versionado SemVer Independiente

Cada paquete independiente dentro de `packages/` (`finances-core`, `finances-connectors`, `finances-ml`, etc.) mantendrá su **ciclo de vida SemVer** propio (`MAJOR.MINOR.PATCH`). Los *breaking changes* exigen *major bump* y plan de migración documentado. El monorepo usa *changesets* o herramienta equivalente para automatizar *changelogs* y *publish*.

## Artículo 11 — C++ High-Performance (PyBind11 Limpio)

Los componentes críticos de cálculo matemático, álgebra lineal densa, o kernels CUDA **DEBEN** implementarse en **C++17/20** y exponerse a Python vía **`pybind11`** con *bindings* mínimos, *RAII* estricto, y conversión *zero-copy* de buffers (NumPy/Polars/Arrow). La interfaz Python es *opaca*; el binario gestiona su memoria.  
**Los kernels CUDA DEBEN operar nativamente sobre `float32` e `int64` (Art. 5) sin conversiones intermedias.**

## Artículo 12 — Idempotencia de Tareas (Reentrancia Segura)

Relanzar una tarea pesada interrumpida (fallo de red, *crash*, *timeout*, reinicio de servicio) **NO DEBE** duplicar registros, corromper estados parciales ni generar resultados inconsistentes. Cada tarea **DEBE** ser idempotente por diseño: claves de idempotencia, transacciones atómicas, y comprobación de estado previo (`PENDING` → `RUNNING` → `COMPLETED`/`FAILED`).

## Artículo 13 — Validación de Datos Canónicos (Pydantic Gatekeeper)

Todo modelo de datos de mercado entrante (OHLCV, *ticks*, *fundamentals*, *order book*) **DEBE** pasar por validación estricta de **Pydantic v2** (`model_validate`, `model_validate_json`) **ANTES** de ser procesado, almacenado o transmitido a motores de cálculo. La validación **DEBE** incluir conformidad con el esquema normalizado del Art. 5 (dtypes, rangos, monotonía temporal). Rechazo temprano (*fail-fast*) con mensaje de error estructurado (Art. 9).

## Artículo 14 — Rate-Limiting Disciplinado (Token Bucket + Backoff)

Las descargas desde APIs externas (brokers, *data vendors*, *on-chain*) **DEBEN** implementar control de tasa mediante algoritmo **Token Bucket** con **backoff exponencial** y *jitter* ante códigos `429` / `5xx`. La configuración (capacidad, recarga, *backoff* base) es parametrizable vía `Pydantic Settings` (prefijo `FT_`).

## Artículo 15 — Documentación Viva (Spec-First)

**CADA** cambio de código funcional **EXIGE** la previa actualización de su especificación técnica asociada (`spec.md`, `plan.md`, `tasks.md` en `specs/ft_<herramienta>/`). No existe *merge* sin *spec* actualizada. La documentación es **contrato**, no apéndice.

## Artículo 16 — Hilos Seguros en Persistencia (Thread-Local / Pool)

Las conexiones a la base de datos embebida (SQLite) **DEBEN** aislarse por hilo (*thread-local storage*) o gestionarse mediante *pool* seguro (`sqlite3.Connection` no es *thread-safe* en modo compartido). `SQLTaskManager` expone API *thread-safe* interna; el uso directo de conexiones crudas fuera del gestor queda prohibido.

## Artículo 17 — Clean Code & Linting (Ruff + MyPy Estricto)

Uso **OBLIGATORIO** de **Ruff** (formateo, *lint*, *isort*) y **MyPy** en modo **`--strict`** en pipeline de CI/CD. *Zero warnings* tolerados. *Type hints* completos en toda API pública y de servicio. *Pre-commit hooks* configurados para bloquear *commits* no conformes.

## Artículo 18 — Garantía de Despacho (Async Dispatch Contract)

El endpoint de ejecución de tareas pesadas (`POST /tasks` o equivalente CLI `run`) **DEBE** responder **inmediatamente** (≤ 200 ms) con un objeto JSON que contenga: `task_id` (UUID v7), `status: "ACCEPTED"`, y `poll_url` (URL absoluta para consultar progreso/resultado vía `GET`). Nada de esperas síncronas.

## Artículo 19 — Control de VRAM (OOM Prevention)

Los binarios CUDA (Art. 11) **DEBEN** implementar control proactivo de VRAM: consultar memoria libre (`cudaMemGetInfo`), fragmentar *batches* dinámicamente, liberar *buffers* intermedios tan pronto dejen de ser referenciados, y exponer métricas de pico/actual a `structlog` (Art. 9). Caídas catastróficas por *Out-Of-Memory* son **inaceptables**.

## Artículo 20 — Criterios de Finalización (Matriz de Validación Cruzada 100%)

Una herramienta (`ft_<nombre>`) se considera **LISTA PARA PRODUCCIÓN** **SI Y SOLO SI** pasa el **100%** de la siguiente matriz de validación cruzada:

| Dimensión | Criterio |
| :--- | :--- |
| **Funcional** | 100% casos de uso EARS cubiertos + tests (Art. 7) |
| **Contractual** | `spec.md` / `plan.md` / `tasks.md` versionados y firmados |
| **Arquitectural** | Dualidad CLI/API verificada (Art. 1), Aislamiento (Art. 4) |
| **Datos** | **Normalización universal float32/int64 (Art. 5), Validación Pydantic (Art. 13)** |
| **Concurrencia** | No bloqueo (Art. 3), Idempotencia (Art. 12), Hilos seguros (Art. 16) |
| **Observabilidad** | Logs duales CLI/servicio (Art. 9), Métricas VRAM (Art. 19) |
| **Resiliencia** | Rate-limit (Art. 14), Backoff, Reintentos, *Circuit Breaker* opcional |
| **Calidad** | Ruff + MyPy strict limpios (Art. 17), Cobertura ≥ 90% |
| **Desempeño** | Benchmarks de regresión dentro de umbral (Polars, C++/CUDA float32) |
| **Despliegue** | Despacho asíncrono + *polling* funcional (Art. 18) |

---

## Gobernanza y Enmiendas

- **Autoridad:** El *Tech Lead* del proyecto es el guardián de esta constitución.
- **Enmiendas:** Requieren RFC documentado, aprobación por consenso del equipo *core*, y bump de versión del documento (`MAJOR` si rompe compatibilidad hacia atrás de proceso).
- **Excepciones:** Ninguna. Si un artículo resulta inaplicable a un caso borde, se abre RFC para enmendar el artículo, no para saltárselo.

---

*Fin del documento.*