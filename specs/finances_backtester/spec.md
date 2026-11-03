# Spec — `finances-backtester` (contratos compartidos, MVP v1.0)

## Contexto e Historias de Usuario

`finances-backtester` es el paquete (`packages/finances-backtester`, aún no existe) que alojará los motores de backtesting de la suite. Cada motor es una herramienta `ft_*` que ejecuta **la misma estrategia** sobre **los mismos datos** con una biblioteca distinta. Para que los resultados sean comparables, este documento fija lo que es común a todos los motores: el **contrato de estrategia** y su **registro**, la **semántica exacta de las señales**, el **modelo de ejecución y de costes**, el **formato de resultados**, las **métricas**, la **identidad de una corrida** y el **contrato de validación cruzada**. Los contratos viven en `finances-core` (Art. 4); cada motor es un adaptador delgado que **solo simula**.

**Una estrategia es una carpeta de código Python local** (`strategies/<kind>/`) que, dado un `DataFrame` de velas de Polars y un modelo Pydantic de parámetros, devuelve señales de entrada y salida booleanas (decisión del usuario, ADR-B09/ADR-B10). Las señales **se calculan en `finances-core`** con Polars `Float32`; los motores reciben `entries`/`exits` ya calculadas y no conocen `kind` ni parámetros. Hay tres estrategias integradas (`sma_cross`, `rsi_reversion`, `donchian_breakout`); el usuario añade las suyas en un directorio local (`FT_STRATEGIES_DIR`). **El código de estrategia nunca se carga por la API** (ejecución arbitraria de código; RF-18). `ft_strategy_manager` ya versiona parámetros con hash SHA-256 (`specs/ft_strategy_manager` RF-1/RF-2); el backtester interpreta esos `parameters` como `{"kind": ..., "params": {...}}`.

El primer motor es `ft_vectorbt` (`specs/ft_vectorbt/`). Los demás (hoja de ruta al final) no se redactan ahora; el requisito de escalabilidad (RF-21) existe para que cada uno solo implemente simulación.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** ejecutar una estrategia versionada sobre un dataset y un rango **para que** obtenga operaciones, curva de capital y métricas reproducibles. |
| US-2 | Backtest Engineer | Como ingeniero de backtesting, **exijo** que cada motor produzca las mismas operaciones y la misma curva de capital (dentro de una tolerancia documentada) para la misma estrategia **para que** las diferencias delaten defectos del adaptador y no del modelo. |
| US-3 | Quant Researcher | Como investigador, **necesito** que relanzar la misma corrida devuelva el resultado ya calculado **para que** no pierda tiempo ni genere duplicados (Art. 12). |
| US-4 | Auditor / Compliance | Como auditor, **exijo** que cada resultado registre la estrategia exacta (id, versión, hash de parámetros, **hash del código**), el dataset, los costes, el motor y su versión **para que** pueda reproducirlo. |
| US-5 | DevOps / Operador | Como operador, **quiero** que todo motor se despache con el mismo contrato asíncrono y se lea con el mismo formato **para que** la UI y los pipelines no dependan del motor. |
| US-6 | Quant Researcher (autor) | Como investigador, **quiero** escribir mi propia estrategia en Python en una carpeta documentada y ejecutarla en cualquier motor **para que** no tenga que tocar el código del motor ni esperar a que se añada al catálogo. |
| US-7 | Operador / Seguridad | Como operador, **exijo** que la API solo ejecute estrategias ya instaladas en el servidor y nunca reciba código **para que** un cliente remoto no pueda ejecutar código arbitrario. |
| US-8 | Backtest Engineer | Como ingeniero, **exijo** que añadir un motor nuevo (`ft_backtesting_py`, `ft_backtrader`, `ft_bt`, `ft_ffn`, `ft_backtestopt_spectre`) solo requiera implementar la simulación **para que** el coste de cada motor no crezca con el número de estrategias. |

---

## Requisitos Funcionales (EARS)

Un **dataset** se identifica como en `ft_data_viewer`: `exchange` / `market_type` / `symbol` / `timeframe` (`DatasetIdentifier`). Una **barra** es una vela; `t` es su índice (0-based) dentro del rango cargado. Las posiciones son **solo largas** (largo o plano). Los requisitos RF-16…RF-22 amplían RF-1/RF-2 (se añaden al final para no renumerar las referencias de `specs/ft_vectorbt/`).

### RF-1 — Registro de Estrategias y Parámetros (Ubícuo) *(reescrito: sustituye al «Catálogo cerrado»)*
**EL SISTEMA DEBE** mantener en `finances_core.backtest.catalog` un registro (`StrategyRegistry`) de las estrategias válidas, construido por el descubrimiento de RF-18 (integradas + locales), y una `CATALOG_VERSION` (SemVer) que identifica la versión del **contrato y de la infraestructura común** (contrato `Strategy` de RF-16, normalización de señales, ayudas compartidas `strategies/_lib`). `CATALOG_VERSION` sube ante cualquier cambio de ese contrato o de las ayudas compartidas; **no** sube por el cambio de una estrategia concreta (cada estrategia se versiona con su `version` y su `code_hash`, RF-19).
- **Contrato de `parameters`** (el campo guardado en `ft_strategy_manager`): exactamente `{"kind": "<kind>", "params": {...}}`; no se admiten otras claves de primer nivel.
- **Estrategias integradas (v1.0.0)**: `sma_cross`, `rsi_reversion`, `donchian_breakout` (semántica normativa en RF-2).
- **SI** `kind` no está en el registro de estrategias válidas, **ENTONCES** error de validación `unknown_kind` (exit 2 / HTTP 400) que lista los `kind` válidos.
- **SI** `kind` existe como carpeta local pero falló su inicialización (RF-18), **ENTONCES** `strategy_init_error` (exit 2 / HTTP 500) con el motivo; las demás estrategias siguen disponibles.
- **SI** `params` incumple el modelo de parámetros de la estrategia (tipo, rango, relación entre parámetros), **ENTONCES** `invalid_params` (exit 2 / HTTP 400) con el detalle por campo.
- El registro es consultable (`kinds` en CLI / `GET /kinds` en API): por estrategia `kind`, `version`, `origin` (`builtin` | `local`), descripción, `code_hash`, `default_params`, JSON Schema de parámetros **generado desde el modelo Pydantic** (fuente única; no existe un `schema.json` aparte), `metrics_target` si lo declara, y para las carpetas locales inválidas su error. Orden determinista por `kind` ascendente.
- Los motores **no** declaran `kind` soportados: no existe `kind_not_supported_by_engine` (RF-21, ADR-B09).

### RF-2 — Semántica Exacta de las Señales (Ubícuo) *(la implementa `finances_core`, no el motor)*
Las señales se evalúan **al cierre** de la barra `t` y producen `entry[t]`/`exit[t]` booleanos. Toda la semántica usa solo datos de las barras `≤ t` (verificado por RF-20). **Calentamiento**: mientras algún dato necesario no esté disponible, la señal es `false` (sin señal), nunca un valor por defecto; el núcleo enmascara además las barras `t < warmup(params)` (RF-16). Las comparaciones son estrictas (`>`/`<`); los empates numéricos no generan señal. La semántica siguiente es **normativa** para las tres estrategias integradas y la implementa `strategies/<kind>/logic.py` en Polars `Float32`; los *golden* (RF-10) la fijan y ningún motor la reimplementa.

**`sma_cross`** — parámetros `fast`, `slow` (enteros, `2 ≤ fast < slow ≤ 10000`):
- `SMA_n(t)` = media aritmética de `close[t-n+1..t]`, definida para `t ≥ n-1`.
- Disponible (`t ≥ slow`, pues exige `t` y `t-1`): `entry[t]` = `SMA_fast(t-1) ≤ SMA_slow(t-1)` **y** `SMA_fast(t) > SMA_slow(t)`; `exit[t]` = `SMA_fast(t-1) ≥ SMA_slow(t-1)` **y** `SMA_fast(t) < SMA_slow(t)`.

**`rsi_reversion`** — parámetros `period` (entero, `2 ≤ period ≤ 1000`), `lower`, `upper` (reales, `0 < lower < upper < 100`):
- RSI de Wilder: `Δ[t] = close[t] - close[t-1]`; `ganancia = max(Δ,0)`, `pérdida = max(-Δ,0)`. Semilla en `t = period`: media simple de las primeras `period` ganancias/pérdidas (`Δ[1..period]`); después `media[t] = (media[t-1]·(period-1) + valor[t]) / period`. `RSI = 100 - 100/(1+RS)`, `RS = media_ganancia/media_pérdida`; **SI** la media de pérdidas es 0 y la de ganancias > 0, **ENTONCES** `RSI = 100`; **SI** ambas son 0, **ENTONCES** `RSI = 50` (consistente con `ft_indicators`). Definido para `t ≥ period`.
- Disponible (`t ≥ period + 1`): `entry[t]` = `RSI[t-1] ≥ lower` **y** `RSI[t] < lower`; `exit[t]` = `RSI[t-1] ≤ upper` **y** `RSI[t] > upper` (reversión a la media: se compra la sobreventa y se vende la sobrecompra).

**`donchian_breakout`** — parámetro `window` (entero, `2 ≤ window ≤ 10000`):
- `alto(t)` = `max(high[t-window..t-1])` y `bajo(t)` = `min(low[t-window..t-1])` (**excluyen** la barra actual), definidos para `t ≥ window`.
- Disponible (`t ≥ window`): `entry[t]` = `close[t] > alto(t)`; `exit[t]` = `close[t] < bajo(t)`.

**Reglas comunes de aplicación** (RF-5): con posición plana, `entry[t]` abre; con posición larga, `exit[t]` cierra; una `entry` con posición larga y una `exit` con posición plana se ignoran. **SI** `entry[t]` y `exit[t]` son verdaderas a la vez (posible en estrategias de usuario), **ENTONCES** el núcleo anula ambas antes de entregarlas al motor (RF-16), de modo que ningún motor tenga que resolver el conflicto.

### RF-3 — Petición de Backtest (Ubícuo)
**EL SISTEMA DEBE** aceptar una única forma canónica `BacktestRequest` (`finances_core.schemas.backtest`) con dos adaptadores equivalentes (CLI y API):
- `dataset` (`exchange`, `market_type`, `symbol`, `timeframe`); `strategy_id` (UUID v7) y `strategy_version` (opcional; **por defecto la última**, como `ft_strategy_manager` RF-2).
- `start_ms`/`end_ms` (`int64`, opcionales; rango `[start_ms, end_ms]` sobre `open_time_ms`, ambos inclusivos; por defecto todo el dataset). `start_ms ≤ end_ms`.
- `initial_cash` (`> 0`, por defecto 10 000), `size_fraction` (`0 < f ≤ 1`, por defecto 1: toda la caja), `fees_bps` (`≥ 0`, por defecto 10), `slippage_bps` (`≥ 0`, por defecto 0).
- `force` (booleano, por defecto `false`; RF-8).
- **Resolución de la estrategia**: la herramienta lee la estrategia mediante `finances_core.backtest.strategy_source` (envoltorio sobre el servicio de `ft_strategy_manager`; ver ADR-B02) y valida `parameters` con RF-1. **SI** `strategy_id` o la versión no existen, **ENTONCES** exit 1 / HTTP 404. **SI** la estrategia está *soft-deleted*, **ENTONCES** se trata como inexistente (exit 1 / 404), coherente con `ft_strategy_manager` Deriva 9.
- `BacktestRequest` (`extra="forbid"`) **no** contiene campos de código, ruta de archivo, nombre de módulo ni directorio de estrategias (RF-18); `--request-file` acepta exactamente el mismo cuerpo.
- Fuera del MVP: varios datasets o varias estrategias por corrida (`datasets`/`strategies` listas) se rechazan por validación.

### RF-4 — Acceso a Datos (Ubícuo)
**EL SISTEMA DEBE** cargar las velas con `finances_core.backtest.candles.read_candles(settings, dataset, start_ms, end_ms)`, que reutiliza el lector y la validación compartidos de `finances_core.indicators_store.source` (`load_source`, `validate_source`; `ft_data_viewer` no es importable, Art. 4) sobre `FT_PARQUET_DIR`, en **solo lectura**.
- Columnas: `open_time_ms` (`Int64`), `open`, `high`, `low`, `close` (`Float32`); el volumen no se usa en v1.0.0. Son exactamente las columnas que recibe `Strategy.signals` (RF-16).
- Validación (Art. 13): orden estrictamente creciente y único, precios finitos y `> 0`. **SI** hay nulos o no finitos en columnas de precio, **ENTONCES** `invalid_source` (exit 2 / HTTP 422); nunca se descartan filas en silencio.
- **SI** el dataset no existe, **ENTONCES** exit 1 / HTTP 404; **SI** el rango no contiene velas, **ENTONCES** `empty_range` (exit 2 / HTTP 400).
- Los huecos (gaps) de datos **no** son un error: las barras consecutivas se simulan tal cual existen en el dataset; la corrida informa `data_gaps` (cantidad de intervalos `> 1,5 × timeframe`) en el manifiesto.
- **SI** hay menos barras que `warmup(params)` de la estrategia más 2, **ENTONCES** la corrida es válida con 0 operaciones y advertencia `insufficient_history`.

### RF-5 — Modelo de Ejecución y Costes (Ubícuo)
**EL SISTEMA DEBE** simular con las reglas siguientes (idénticas en todos los motores):
1. **Sin anticipación**: la señal de la barra `t` (cierre) se ejecuta al **precio de apertura de la barra `t+1`**. Una señal en la última barra no se ejecuta. Una entrada que se ejecutaría en la última barra se ignora (no abre posiciones de cero duración).
2. **Órdenes**: solo de mercado, de tamaño completo (entrada = comprar; salida = vender toda la posición). Sin cortos, apalancamiento, parciales ni intrabarra.
3. **Precio de ejecución**: compra `open·(1 + s)`, venta `open·(1 - s)`, con `s = slippage_bps / 10 000`.
4. **Comisiones**: `fees = fee_rate · (precio_ejecución · cantidad)` en cada ejecución, con `fee_rate = fees_bps / 10 000`, descontadas de la caja.
5. **Tamaño**: `cantidad = size_fraction · caja / (precio_ejecución · (1 + fee_rate))` (cantidades fraccionarias; la caja cubre las comisiones). Con `size_fraction = 1` la caja queda en 0 tras entrar.
6. **Liquidación final**: **SI** hay posición larga al terminar el rango, **ENTONCES** se cierra al **precio de cierre de la última barra** (con el mismo coste de venta), `exit_reason = 1` (`end_of_data`).
7. **Capital**: `equity[t] = caja[t] + cantidad[t] · close[t]` al cierre de cada barra (valoración a mercado).

El desplazamiento `t → t+1` (regla 1) es responsabilidad del motor: recibe las señales «al cierre de `t`» tal como las produce el núcleo.

### RF-6 — Resultados Canónicos (Ubícuo)
**EL SISTEMA DEBE** producir por corrida dos tablas con dtypes del Art. 5 (validados al escribir; un valor fuera de dominio es error `output_out_of_range`, nunca se trunca):

`trades` (una fila por operación completa, ordenada por `entry_time_ms`):
| Columna | dtype | Significado |
|---|---|---|
| `entry_time_ms` | `Int64` | `open_time_ms` de la barra donde se ejecuta la entrada |
| `exit_time_ms` | `Int64` | `open_time_ms` de la barra donde se ejecuta la salida (en la liquidación final, la última barra) |
| `side` | `Int8` | dominio `{1}` (largo; `-1` reservado para cortos fuera del MVP) |
| `entry_price`, `exit_price` | `Float32` | precios de ejecución (con deslizamiento) |
| `size` | `Float32` | cantidad |
| `fees` | `Float32` | comisión de entrada + salida |
| `pnl` | `Float32` | `(exit_price - entry_price)·size - fees` |
| `return` | `Float32` | `pnl / (entry_price·size + comisión de entrada)` |
| `exit_reason` | `Int8` | dominio `{0 señal, 1 end_of_data}` (documentado, Art. 5.1 bis) |

`equity` (una fila por barra del rango, ordenada):
| Columna | dtype | Significado |
|---|---|---|
| `open_time_ms` | `Int64` | tiempo de apertura de la barra |
| `equity` | `Float32` | valor de la cartera al cierre de la barra |
| `drawdown` | `Float32` | `equity / máximo_acumulado(equity) - 1` (≤ 0); el máximo incluye `initial_cash` |

### RF-7 — Métricas del Resumen (Ubícuo)
**EL SISTEMA DEBE** calcular las métricas en `finances_core.backtest.metrics` (no en el motor) desde `equity` y `trades` canónicos, de modo que sean idénticas para todos los motores. `tf_ms` es la duración del timeframe y `P = 365,25·86 400 000 / tf_ms` los periodos por año (calendario continuo; ver A8). Con `r[t] = equity[t]/equity[t-1] - 1` y `equity[-1] = initial_cash`:

| Métrica | Fórmula |
|---|---|
| `total_return` | `equity[último]/initial_cash - 1` |
| `cagr` | `(equity[último]/initial_cash)^(1/años) - 1`, `años = n_barras / P`; `-1` si `equity[último] ≤ 0` |
| `max_drawdown` | `min(drawdown)` (≤ 0) |
| `sharpe` | `media(r)/desv(r, ddof=1)·√P`, tasa libre de riesgo 0; `null` si `desv = 0` o hay < 2 barras |
| `sortino` | `media(r)/√media(min(r,0)²)·√P`; `null` si el denominador es 0 |
| `win_rate` | operaciones con `pnl > 0` / `n_trades`; `null` si `n_trades = 0` |
| `n_trades` | número de filas de `trades` |
| `exposure` | barras con posición larga al cierre / `n_barras` |

El resumen (`summary.json`) incluye además `initial_cash`, `final_equity`, `n_bars`, `start_ms`, `end_ms` efectivos y las advertencias (`insufficient_history`, `data_gaps`). Las métricas se serializan como números JSON desde valores `Float32`; la precisión de las acumulaciones se verifica con un test (RFC-0004, tolerancia 1e-3).

### RF-8 — Identidad e Idempotencia (Ubícuo, Art. 12) *(campos de estrategia ampliados)*
**EL SISTEMA DEBE** calcular `run_id` como SHA-256 (hex) del JSON canónico (claves ordenadas, sin espacios) de: `strategy_hash` (de `ft_strategy_manager`; cubre `kind` y `params`), `strategy_id` y versión resueltas, **`kind_version` y `strategy_code_hash` (RF-19)**, dataset, `start_ms`/`end_ms` **efectivos**, `data_hash` (SHA-256 del contenido de las columnas de velas del rango, ver A10), `initial_cash`, `size_fraction`, `fees_bps`, `slippage_bps`, `engine` (nombre), `engine_version` (versión de la biblioteca), `CATALOG_VERSION` y `RESULT_SCHEMA_VERSION`.
- Editar el código de una estrategia local (aunque `kind`, `params` y `strategy_hash` no cambien) **DEBE** cambiar el `run_id`; nunca se sirve un resultado de una versión anterior del código.
- **SI** ya existe un resultado completo con ese `run_id`, **ENTONCES** se devuelve sin recalcular (`cached = true`) y **sin calcular señales**, salvo `force = true`.
- Con `force = true` se recalcula y se reemplaza de forma atómica; el contenido de `trades.parquet`, `equity.parquet` y `summary.json` DEBE ser idéntico al anterior (determinismo, RF-5 de `ft_vectorbt`).
- Dos tareas concurrentes con el mismo `run_id` son seguras: cada una escribe en un directorio temporal propio y la que termina segunda descarta el suyo y devuelve el existente.

### RF-9 — Almacén de Resultados (Ubícuo)
**EL SISTEMA DEBE** persistir cada corrida en
`{FT_RESULTS_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/{run_id}/` (ver A11) con `trades.parquet`, `equity.parquet`, `summary.json` y `manifest.json`.
- `manifest.json` es el **marcador de completitud** y se escribe el último: petición resuelta, `strategy_id`/`version`/`hash`/`kind`/`params`, **`kind_version`, `strategy_code_hash`, `strategy_origin`**, dataset, rango efectivo, `data_hash`, costes, `engine`/`engine_version`, `CATALOG_VERSION`, `RESULT_SCHEMA_VERSION`, `created_at_ms`, `duration_ms`, versión de Python y de las bibliotecas relevantes, advertencias.
- Escritura atómica: se escribe en `{run_id}.tmp-{uuid}` hermano y se renombra; un directorio sin `manifest.json` no se considera resultado y se ignora (y se limpia al recalcular). Parquet con compresión fija (`zstd`) y sin metadatos dependientes del tiempo, para reproducibilidad byte a byte.
- Lectura: listado por filtros (`exchange`, `market_type`, `symbol`, `timeframe`, `strategy_id`, `engine`) con paginación por cursor; lectura de `trades`/`equity` paginada por `entry_time_ms`/`open_time_ms` (`after_ms`, `limit` 1..10000).
- `FT_RESULTS_DIR` es compartido por todos los motores y por otros resultados de la suite; el almacén solo reconoce directorios con `manifest.json` válido.

### RF-10 — Contrato de Motor y Validación Cruzada (Ubícuo) *(el motor recibe señales, no estrategia)*
**EL SISTEMA DEBE** definir en `finances_core.backtest.engine_api` el contrato `BacktestEngine`: `name`, `version() -> str` y `run(candles, signals, costs, sizing) -> EngineOutput` (`trades` y `equity` canónicos, RF-6) más `validate_engine_output`. `signals` es el `Signals` normalizado de RF-16 (`entries`/`exits` booleanas, misma longitud que `candles`). El contrato **no** incluye `supported_kinds`, `kind`, `params` ni la estrategia resuelta.
- **Orquestación común**: `finances_core.backtest.runner.run_backtest(request, engine, settings, on_phase)` ejecuta resolver estrategia → cargar velas → identidad (`run_id`) → ¿existe? → **calcular y normalizar señales** → motor → validar → métricas → persistir, de modo que `service.py` de cada motor solo aporta el motor.
- **Contrato de validación cruzada**: todo motor **DEBE** pasar la suite de contrato `tests/contract/` del paquete sobre un **dataset de fixture determinista** (semilla fija, ≥ 2 000 barras, con tendencia y rango y sin empates numéricos en los umbrales) frente a *golden files* (señales, operaciones, equity y métricas) generados por una referencia independiente **de prueba** (fuera de `src/`), para las tres estrategias integradas. Criterios: mismas operaciones (`n_trades`, `entry_time_ms`, `exit_time_ms`, `side`, `exit_reason` exactos) y equity/precios/pnl/métricas dentro de la tolerancia de RFC-0004 (punto 5). Una diferencia es un defecto del adaptador.
- La suite incluye además una **estrategia sintética de prueba** (`tests/`, no integrada) que demuestra RF-21: el motor ejecuta señales arbitrarias sin código específico de estrategia.
- Con un segundo motor la misma suite compara además motor contra motor sin cambios.

### RF-11 — Frontera Numérica (Ubícuo, RFC-0004)
**EL SISTEMA DEBE** mantener `float32`/`int64` en todos los contratos, el almacén, las métricas, las estrategias y los módulos de servicio (Art. 5, 8). Los motores basados en `pandas`/NumPy confinan lo que necesitan al módulo `engine.py` con dos puntos de conversión (RFC-0004). Un test AST por motor y otro para `finances_core/backtest/` (incluido `strategies/`) lo verifican; **`pandas` y NumPy tampoco aparecen en ninguna estrategia** (Art. 8: Polars por defecto; RFC-0004 solo exceptúa el `engine.py` de los motores). Las estrategias locales se revisan con el mismo análisis AST en el descubrimiento (RF-18).
- **SI** RFC-0004 no está aprobada (y el Art. 8 enmendado), **ENTONCES** ningún motor basado en `pandas` se fusiona.

### RF-12 — Configuración vía `FT_` Settings (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings` (prefijo `FT_`): `FT_PARQUET_DIR`, `FT_RESULTS_DIR`, `FT_TASKS_DB_PATH`, `FT_STRATEGIES_DB_PATH` (o `FT_SHARE_DIR/strategies.db`), **`FT_STRATEGIES_DIR`** y `FT_LOG_*`.
- `FT_STRATEGIES_DIR` **ya existe** en `FinancesSettings` y `ft_setup` («Directorio de definiciones de estrategias», por defecto `./strategies`) sin ningún consumidor; este ciclo le da significado: raíz de las estrategias Python **locales** (`FT_STRATEGIES_DIR/<kind>/logic.py`), resuelta a ruta absoluta respecto de la raíz del proyecto (A21). Su descripción en `ft_setup` se actualiza (tarea P3).
- Variable **nueva**: `FT_BACKTEST_MAX_CONCURRENT` (entero 1..16, por defecto 2): corridas simultáneas por proceso; una más espera en cola. Debe registrarse en `ft_setup/env_list.conf` (ver `tasks.md`, «Cambios en otros documentos»).
- Ni la CLI ni la API aceptan un directorio de estrategias alternativo: solo la variable de entorno del proceso.

### RF-13 — Logging Dual y Auditoría (Ubícuo)
**EL SISTEMA DEBE** usar `structlog` (Art. 9). Cada corrida registra un evento INFO de auditoría `backtest_run` con `task_id`, `run_id`, `engine`, `engine_version`, `strategy_id`, `strategy_version`, `strategy_hash`, `kind`, **`kind_version`, `strategy_code_hash`, `strategy_origin`**, `dataset`, rango, `n_bars`, `n_trades`, `cached`, `forced`, `duration_ms` y `result` (`completed` | `cached` | `failed`). Métricas: `load_ms`, **`signals_ms`**, `engine_ms`, `persist_ms`. CLI: tablas Rich; API: JSON con `component="ft_<motor>"`. Al descubrir estrategias locales se emite un evento INFO `strategies_discovered` con `kind`, `origin`, `code_hash` y los errores de las inválidas.

### RF-14 — Errores y Códigos de Salida (Evento-Driven)
Tabla común a todos los motores (cada `ft_*` la reproduce en su spec):
| Condición | CLI exit | API |
|-----------|----------|-----|
| OK (incluye resultado en caché y 0 operaciones) | 0 | 200 / 202 |
| Dataset, estrategia/versión, corrida o tarea no encontrada | 1 | 404 |
| `unknown_kind`, `invalid_params`, rango inválido o vacío, costes inválidos, `invalid_source` | 2 | 400 / 422 |
| `strategy_init_error` (carpeta local inválida o colisión de nombre, RF-18) | 2 | 500 |
| Tarea `FAILED` (excepción inesperada del motor, `output_out_of_range`, `strategy_runtime_error` —excepción dentro de `signals`—, `invalid_signals` —dtype, longitud o nulos incorrectos—) | 3 | 500 en `/result` |
| `/result` de una tarea no terminada | — | 409 |
| Error de IO (disco, permisos) | 4 | 500 |

`kind_not_supported_by_engine` **se elimina** (RF-21).

### RF-15 — Aislamiento (No deseado, Art. 4)
**SI** un motor importa de otro motor `ft_*`, de `finances_downloader`, `finances_analytics`, `ft_data_viewer` o `finances_api`, **ENTONCES** es una violación del Art. 4 (test AST por motor). Los motores solo importan `finances_core` y su propia biblioteca. `finances-api` importa el `router` público de cada motor (excepción del Art. 4).
- **SI** un módulo de motor importa `finances_core.backtest.strategies` o `finances_core.backtest.catalog`, **ENTONCES** es una violación (RF-21): el motor solo conoce `Signals` a través de `engine_api`.
- **SI** una estrategia (integrada o local) importa algo fuera de la lista permitida de RF-18, **ENTONCES** es un error de inicialización (test AST y guarda en el descubrimiento).

### RF-16 — Contrato `Strategy` (Ubícuo)
**EL SISTEMA DEBE** definir en `finances_core.backtest.strategies` la clase base `BaseStrategy` con:
- `kind` (`str`, `^[a-z][a-z0-9_]{2,39}$`, **igual al nombre de la carpeta**), `version` (SemVer de la lógica), `description` (una línea).
- `Params`: modelo Pydantic con `model_config = ConfigDict(extra="forbid")`, **fuente única** de tipos, rangos y relaciones (validadores); el JSON Schema se exporta desde él (RF-1).
- `default_params`: valores por defecto que **DEBEN** validar contra `Params`.
- `warmup(params) -> int`: primer índice `t` con señal posible; el núcleo fuerza `false` en `t < warmup` y lo usa para `insufficient_history` (RF-4).
- `signals(candles: pl.DataFrame, params) -> Signals`: función **pura y determinista** que recibe las velas de RF-4 (`Float32`/`Int64`) y devuelve `Signals(entries, exits)`, dos `pl.Series` `Boolean` de la misma longitud que `candles` (pueden construirse con expresiones Polars). Solo Polars `Float32`; sin `pandas`/NumPy/`float64` (RF-11), sin E/S, sin red, sin estado entre llamadas, sin aleatoriedad no sembrada, sin leer fuera de `candles`.
- `metrics_target` (opcional): `MetricsTarget(metric, direction)` con `metric` ∈ nombres de RF-7 y `direction` ∈ `{max, min}`; es **solo informativo** en este ciclo (objetivo declarado para una futura optimización); no cambia la ejecución.
- **Normalización de señales** (`signals.finalize`, común a todos los motores): **SI** `entries`/`exits` no son `Boolean` o su longitud difiere de `candles.height` o hay nulos en `t ≥ warmup`, **ENTONCES** `invalid_signals`; los nulos y valores con `t < warmup` pasan a `false`; las barras con `entry` y `exit` simultáneas pasan ambas a `false`.
- **SI** `signals` lanza una excepción, **ENTONCES** `strategy_runtime_error` (la tarea falla; el detalle se registra en el log, sin trazas en la respuesta de la API).
- Ámbito: una estrategia produce señales **largo/plano** sin estado de ejecución; no conoce el efectivo, la posición abierta, los costes ni el precio de entrada (límites en ADR-B09).

### RF-17 — Estructura de Carpeta y Documentación por Estrategia (Ubícuo)
**EL SISTEMA DEBE** exigir que cada estrategia sea una carpeta `strategies/<kind>/` con:
- `logic.py` (obligatorio): define exactamente una subclase concreta de `BaseStrategy` (`Params`, `default_params`, `warmup`, `signals`, `version`, `description`, `metrics_target` opcional). Puede importar módulos hermanos de su propia carpeta.
- `README.md` (obligatorio, en español), con estas secciones de nivel 2, en este orden y no vacías: `## Lógica de negocio` (intención y cuándo opera / cuándo no), `## Matemática` (fórmulas exactas, calentamiento, empates), `## Diagrama` (al menos un bloque ```` ```mermaid ```` de estados o flujo —`stateDiagram-v2` o `flowchart`—), `## Diccionario de parámetros` (tabla `Parámetro | Tipo | Rango | Defecto | Descripción` con **una fila por campo de `Params`**, y defectos iguales a `default_params`). Secciones adicionales opcionales (`## Limitaciones`, `## Ejemplo`).
- **SI** falta `logic.py` o `README.md`, o falta/está vacía una sección obligatoria, o la tabla no coincide con `Params`, o `kind` ≠ nombre de carpeta, **ENTONCES** error de inicialización de esa estrategia (`strategy_init_error`) con el archivo y la causa. Un test lo exige para las integradas (RF-20) y `kinds --check` para las locales.

### RF-18 — Descubrimiento, Carga y Frontera de Seguridad (Ubícuo)
**EL SISTEMA DEBE** descubrir estrategias en dos orígenes: **integradas** (carpeta `finances_core/backtest/strategies/`) y **locales** (`FT_STRATEGIES_DIR/<kind>/`), ignorando entradas que no sean directorios o cuyo nombre empiece por `_` o `.` (`_lib`, `_template`, `__pycache__`).
- **Orden determinista**: el registro se ordena por `kind` ascendente (orden de bytes), independiente del orden del sistema de archivos.
- **Colisión de nombres**: **SI** una carpeta local tiene el mismo `kind` que una integrada, **ENTONCES** la local queda inválida con `kind_collision`; una estrategia integrada nunca se sustituye ni se «sombrea».
- **Error de inicialización limpio**: **SI** una carpeta falla (falta un archivo obligatorio, README incompleto, import AST prohibido, `kind` ≠ carpeta, `default_params` inválidos, 0 o más de 1 clase concreta, excepción al importar), **ENTONCES** esa estrategia queda registrada como inválida con un mensaje legible (archivo y causa, sin traza), las demás siguen disponibles, y su uso produce `strategy_init_error` (RF-1). **SI** la inválida es **integrada**, **ENTONCES** es un defecto del paquete y la construcción del registro falla (exit 3).
- **Guarda estática**: antes de importar, un análisis AST de todos los `*.py` de la carpeta **DEBE** rechazar importaciones fuera de la lista permitida (`polars`, `pydantic`, `math`, `typing`, `dataclasses`, `enum`, `functools`, `itertools`, `collections`, `decimal`, `finances_core.backtest.strategies` y módulos hermanos de la propia carpeta), `pandas`, NumPy, nombres de doble precisión (RF-11) y las llamadas `eval`, `exec`, `compile`, `__import__`, `open`. Es una **guarda de buenas prácticas, no un sandbox**.
- **Carga**: por `importlib` desde el archivo, con nombre de módulo sintético por origen y `kind`, sin modificar `sys.path`.
- **Frontera de seguridad (ADR-B10)**: el código local se ejecuta en el proceso con los privilegios del usuario; quien escribe en `FT_STRATEGIES_DIR` puede ejecutar código. Por tanto **EL SISTEMA NO DEBE** aceptar, almacenar ni importar código de estrategia recibido por la API ni por `--request-file`, ni rutas, nombres de módulo o directorios de código en ninguna petición. La API **SOLO** lista (`GET /kinds`) y ejecuta estrategias ya instaladas en el servidor. Aceptar código por red es una violación de este requisito.
- **Ciclo de vida**: el registro se construye al iniciar el proceso (CLI: cada invocación; `ft_serve`: al arrancar) y no se recarga en caliente; añadir o editar una estrategia local requiere reiniciar el servicio. El descubrimiento importa (ejecuta el nivel superior de) todas las carpetas locales que pasan la guarda estática; por ello el directorio debe ser de confianza.

### RF-19 — Reproducibilidad del Código de Estrategia (Ubícuo, Art. 12)
**EL SISTEMA DEBE** calcular `strategy_code_hash` = SHA-256 de la concatenación, ordenada por ruta POSIX relativa, de `ruta ⏎ 0x00 ⏎ contenido ⏎ 0x00` de cada `*.py` de la carpeta de la estrategia (finales de línea normalizados a LF; se excluyen `README.md` y `__pycache__`), y registrarlo con `kind_version` y `strategy_origin` en `run_id` (RF-8), manifiesto (RF-9) y auditoría (RF-13).
- Cambiar solo el `README.md` **no** cambia el `run_id`; cambiar cualquier `*.py` de la carpeta sí.
- Las ayudas compartidas `strategies/_lib` **no** entran en el hash de cada estrategia: su cambio de semántica se refleja subiendo `CATALOG_VERSION` (RF-1). Las estrategias locales pueden usar `_lib` o ser autosuficientes.
- `ft_strategy_manager` sigue guardando solo `{"kind","params"}`; el hash del código no se almacena allí.

### RF-20 — Verificación Automática de Estrategias (Ubícuo, Art. 7)
**EL SISTEMA DEBE** proveer `finances_core.backtest.strategies.testing` (`assert_strategy_contract(kind)`) y ejecutarlo para **cada** estrategia integrada en la suite de `finances-core`; el comando `kinds --check` lo ejecuta para las locales. Comprueba:
1. **Estructura y README** (RF-17).
2. **Guarda AST** (RF-11, RF-18).
3. **Contrato de señales**: `Boolean`, longitud correcta, sin nulos tras `warmup`, `default_params` válidos, determinismo (dos llamadas idénticas), no muta `candles`.
4. **Sin anticipación (look-ahead)**: sobre una serie sintética determinista y para varios puntos de corte `k` (al menos `warmup`, `warmup+1`, `n/4`, `n/2`, `3n/4`, `n-1`), `signals(candles[:k])` **DEBE** ser idéntico a `signals(candles)[:k]` para `entries` y `exits`: la señal en la barra `t` no cambia si se truncan las barras posteriores.
- Una estrategia de prueba con anticipación deliberada (`shift(-1)`) **DEBE** hacer fallar la comprobación 4 (test de mutación de la propia suite).
- Una estrategia local que no pase `kinds --check` no se rechaza al ejecutar (la comprobación 4 es una herramienta de autor, no una guarda de ejecución); sí se aplican siempre RF-17/RF-18 y la normalización de RF-16.

### RF-21 — Neutralidad respecto del Motor y Escalabilidad (Ubícuo)
**EL SISTEMA DEBE** garantizar que añadir un motor (`ft_backtesting_py`, `ft_backtrader`, `ft_bt`, `ft_ffn`, `ft_backtestopt_spectre`) solo exija implementar el contrato `BacktestEngine` de RF-10 —conversión de entrada, simulación según RF-5, conversión de salida a RF-6— y pasar la suite de contrato, **sin escribir ni mantener código por estrategia**. Se verifica con:
- Un test que inspecciona la firma de `BacktestEngine.run` (sin `kind`, `params` ni estrategia) y un test AST de que ningún módulo de motor importa `finances_core.backtest.strategies`/`catalog` (RF-15).
- La estrategia sintética de la suite de contrato (RF-10).
- Añadir una estrategia (integrada o local) no requiere modificar ningún motor ni su suite de contrato.
- **Límites declarados (ADR-B09)**: las estrategias que dependen del estado de ejecución (stops sobre el precio de entrada, tamaño dinámico, piramidación, órdenes límite, intrabarra, cortos, multiactivo) no se expresan como `(entries, exits)` y quedan fuera de este contrato. Una extensión futura —**no implementada ni especificada aquí**— permitiría que un motor declare estrategias *nativas del motor* con su propio RFC/spec; mientras tanto el SISTEMA DEBE rechazar su registro.

### RF-22 — Guía de Autor de Estrategias (Ubícuo, Art. 15)
**EL SISTEMA DEBE** incluir una guía paso a paso `docs/guides/strategy-authoring.md` (en español) con: prerrequisitos y configuración de `FT_STRATEGIES_DIR`; **boilerplate** copiable (la carpeta `strategies/_template/` con `logic.py` y `README.md` válidos y comentados); pasos numerados (crear carpeta, definir `Params` y `default_params`, escribir `warmup` y `signals`, documentar el README, ejecutar `kinds --check`, crear la estrategia en `ft_strategy_manager` con `{"kind","params"}`, ejecutar `run`); **checklist** de verificación previa a usar la estrategia; **ejemplos** (las tres integradas y un ejemplo mínimo nuevo); errores frecuentes (anticipación, `shift` negativo, nulos, `pandas`, estado entre llamadas) y qué mensaje produce cada uno; y una advertencia de seguridad (RF-18).
- **SI** un paso o ejemplo de la guía deja de ejecutarse tal cual contra un `FT_STRATEGIES_DIR` temporal, **ENTONCES** el test de la guía falla.

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Cortos, apalancamiento, carteras multi-activo** | **Fuera de alcance MVP**. Una corrida = un dataset y una estrategia, largo/plano. `side` reserva `-1`. |
| **Ejecución intrabarra, tipos de orden distintos del mercado, órdenes parciales** | **Fuera de alcance**. |
| **Estrategias con estado de ejecución** (stop-loss/trailing sobre el precio de entrada, tamaño dinámico, piramidación) | **Fuera de alcance**: el contrato es `(entries, exits)` sin estado (RF-16, ADR-B09). Posible extensión futura de estrategias nativas del motor, sin especificar. |
| **Optimización de parámetros, *walk-forward*, barridos** | **Fuera de alcance**: pertenece a `ft_backtestopt_spectre` (motor GPU). `metrics_target` solo declara el objetivo. |
| **Trading en vivo o *paper*** | **Fuera de alcance**: bots (`ft_bot_*`). |
| **Código de estrategia por API** (subida, `POST` de código, rutas o módulos en la petición) | **Prohibido** (RF-18, ADR-B10): ejecución arbitraria de código. |
| **Estrategias Python locales** | **Permitidas** solo desde las carpetas integradas y `FT_STRATEGIES_DIR` (CLI/configuración local); sin sandbox ni aislamiento de proceso (fuera de alcance; A23). |
| **Recarga en caliente de estrategias** | **Fuera de alcance**: requiere reiniciar el proceso. |
| **Parser de fórmulas / *Formulaic Alphas*, DSL de reglas** | **Fuera de alcance de esta ronda**: tridente posterior `formulaic_alpha` (ver «Futuro»). |
| **Indicadores desde el almacén de `ft_indicators`** | **Fuera de alcance MVP**. Cada estrategia calcula sus señales desde las velas (semántica de RF-2). Una ruta opcional futura leería columnas del almacén con el lector compartido (`finances_core.indicators_store`); no entra ahora porque exigiría que el usuario precalcule series con parámetros idénticos, acopla la corrida al estado de otro almacén (cambia `run_id` y reproducibilidad) y las definiciones de RSI/SMA de `ft_indicators` deberían congelarse como contrato. |
| **Gráficos** | **Fuera de alcance**. |
| **Varias fuentes de datos fusionadas, remuestreo de timeframes** | **Fuera de alcance**. Un dataset del almacén, tal cual. |
| **Calendarios bursátiles y anualización por sesiones** | **Fuera de alcance MVP** (A8). |

## Futuro: tridente `formulaic_alpha` (no se redacta ahora)

Tras esta ronda se redactará un tridente aparte para evaluar fórmulas tipo *Formulaic Alphas* de Kakushadze (PDF en `docs/articles/`) mediante un parser AST con lista blanca sobre una representación intermedia de datos puros (sin `eval`), que produzca el mismo `Signals` del contrato RF-16 y, por tanto, funcione en todos los motores sin cambios. Notas ya verificadas contra el PDF:
- Fórmulas citadas: **Alpha#1** `rank(Ts_ArgMax(SignedPower(((returns<0)?stddev(returns,20):close),2.),5))-0.5`; **Alpha#3** `-1*correlation(rank(open),rank(volume),10)`; **Alpha#5** `rank(open-sum(vwap,10)/10)*(-1*abs(rank(close-vwap)))`; **Alpha#101** `(close-open)/((high-low)+.001)`. (La conversación de `ai/` confunde #3 con #5 y escribe mal #1; esta es la versión del PDF.)
- `rank` es **transversal** (entre activos), y `vwap`, `cap` y la clasificación por industria no están disponibles para OHLCV de un solo símbolo: solo los operadores de serie temporal aplican a una corrida mono-símbolo (el volumen tampoco se carga en v1.0.0, RF-4).
- Una fórmula produce un **puntaje**, no señales: el tridente futuro debe **definir la regla puntaje → posición** (umbrales, cruces, signo) antes de poder usarse como `Signals`.
- Un motor GPU (`ft_backtestopt_spectre`) y la validación de estrategias nativas son temas de RFC aparte.

## Hoja de Ruta de Motores (no se redactan ahora)

| Orden | Herramienta | Biblioteca / nota |
|---|---|---|
| 1 | `ft_vectorbt` | VectorBT — **este ciclo** (`specs/ft_vectorbt/`) |
| 2 | `ft_backtesting_py` | backtesting.py; mismo contrato, RFC-0004 ya cubre su `engine.py`; solo simulación (RF-21) |
| 3 | `ft_backtrader` | Backtrader; orientado a eventos, puede requerir adaptación de las reglas de RF-5; alimenta las señales precalculadas (RF-21) |
| 4 | `ft_bt` | bt (carteras); fuera de las estrategias del MVP hasta que se admitan carteras |
| 5 | `ft_ffn` | ffn: métricas de rendimiento, no un simulador; se evaluará como contraste de RF-7 |
| 6 | `ft_backtestopt_spectre` | C++/CUDA, `float32` nativo, optimización; RFC propio (Art. 11, 19); recibe las señales del núcleo como oráculo y, para barridos, podría requerir estrategias nativas (ADR-B09) |
| 7 | `ft_bot_paper_binance`, `ft_live_bot` | Bots; fuera del alcance de backtesting |

Cada motor nuevo añade su propio trío `specs/ft_<motor>/` y reutiliza este paquete de contratos y la suite de RF-10. **No** escribe código por estrategia (RF-21).

---

## Supuestos y Decisiones Abiertas

**Decisiones tomadas por el usuario (requisitos, no se discuten):** (1) las señales se calculan en el **núcleo** con Polars `Float32` y los motores solo simulan; (2) esta ronda admite **estrategias Python locales** (integradas + `FT_STRATEGIES_DIR`), nunca código cargado por la API; el parser de fórmulas queda para un tridente posterior. *(Sustituye a la decisión previa «catálogo cerrado, sin plugins ni DSL».)*

Todo lo siguiente es **propuesta del diseño** (marcada como supuesto) y está sujeto a veto. Las filas marcadas *(cambiado)* o *(nuevo)* se modificaron en esta ronda:

| ID | Supuesto / decisión propuesta | Consecuencia si se veta |
|----|-------------------------------|--------------------------|
| A1 *(cambiado)* | Contratos compartidos en `finances_core/backtest/` (registro y contrato de estrategias, estrategias integradas, costes, métricas, identidad, almacén, runner, contrato de motor) y `finances_core/schemas/backtest.py`. | Cada motor duplicaría el pipeline y las señales; se rompe el contrato común. |
| A2 | Los motores leen la estrategia vía `finances_core.backtest.strategy_source` (envoltorio sobre `ft_strategy_manager`). Verificado: `ft_strategy_manager` vive en `finances-core`, pero los tests AST de las demás herramientas prohíben todo import de raíz `ft_*`; el envoltorio mantiene intacta esa regla. | Habría que permitir `ft_strategy_manager` como excepción explícita en los tests AST y en el Art. 4. |
| A3 | El lector de velas se construye sobre `indicators_store.source` (`load_source`/`validate_source`) más filtro de rango. | Escribir un lector nuevo duplicaría la validación. |
| A4 | Alcance MVP: un dataset, una estrategia, largo/plano, caja inicial, fracción fija, `fees_bps`, `slippage_bps`. | — |
| A5 | Ejecución: señal al cierre de `t` → apertura de `t+1` (la desplaza el motor); entradas en la última barra ignoradas; liquidación final al **cierre** de la última barra con `exit_reason = 1`. | Cambia RF-5/RF-6 y los *golden*. |
| A6 *(cambiado)* | Semántica de señales de RF-2, ahora implementada en el núcleo (Polars `Float32`): cruces (eventos) para `sma_cross` y `rsi_reversion`; condición (estado) para `donchian_breakout`; RSI de Wilder con semilla SMA; Donchian excluye la barra actual y usa la misma `window` para entrada y salida. La recursión de Wilder en `Float32` se valida contra los *golden* (referencia de prueba en doble precisión permitida solo en `tests/`); si diverge en un umbral, se documenta y se ajusta el *fixture* o se justifica con RFC. | Cambia la `version` de la estrategia, el `code_hash` y los *golden*. |
| A7 | Valores por defecto: `initial_cash = 10 000`, `size_fraction = 1`, `fees_bps = 10`, `slippage_bps = 0`. | Solo cambia la petición por defecto. |
| A8 | Anualización con año de calendario continuo (365,25 días, mercado 24/7), tasa libre de riesgo 0; incorrecta para acciones con sesiones (sobreestima `P`). | Requiere calendario por mercado (fuera de MVP). |
| A9 | Conjunto de métricas de RF-7 (incluye `cagr`) calculado en `finances_core`, no en el motor. | Cada motor tendría sus fórmulas y no serían comparables. |
| A10 *(cambiado)* | `run_id` incluye `data_hash`, `RESULT_SCHEMA_VERSION`, `kind_version` y `strategy_code_hash`, además de lo pedido: así reingestar o corregir velas, o editar el código de una estrategia, no devuelve un resultado obsoleto. Coste: hashear las columnas del rango y los `*.py` de la estrategia en cada petición. | `run_id` más estable pero potencialmente obsoleto ante cambios de datos o de código. |
| A11 | Ruta de resultados con `market_type=` (`{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/{run_id}/`), coherente con los demás almacenes, en lugar de `{exchange}/{symbol}/{timeframe}/{run_id}` (que mezcla `spot` y `futures` del mismo símbolo). | Volver a la ruta sin `market_type`. |
| A12 | `trades` añade `exit_reason` (`Int8`) a las columnas pedidas, para distinguir la liquidación final. | Eliminarla y tratar la liquidación final como salida normal. |
| A13 | Con un solo motor, la «validación cruzada» se hace contra *golden files* de una referencia independiente de prueba; la comparación motor-contra-motor llega con el segundo motor. | — |
| A14 | Tolerancias iniciales de RFC-0004 (punto 5), a calibrar con evidencia. | — |
| A15 | Única variable nueva: `FT_BACKTEST_MAX_CONCURRENT` (`FT_STRATEGIES_DIR` ya existía; A21). | — |
| A16 | Paquete `requires-python = ">=3.11,<3.14"` hasta que V0.2 de `ft_vectorbt` pruebe la matriz; la `.venv` local usa Python 3.14 y pandas 3.x, probablemente incompatibles con VectorBT. Versión de `vectorbt`: **pin a verificar** (no se inventa). | Ajustar la matriz de CI y la `.venv`. |
| A17 | RFC-0004 enmienda el Art. 8 (constitución 1.2.0) para admitir `pandas` en `engine.py`, porque el Art. 8 no prevé excepciones por RFC. Las estrategias **no** están cubiertas por la excepción. | Sin enmienda, no hay motores basados en `pandas`. |
| A18 | Estrategias *soft-deleted* se tratan como inexistentes al ejecutar. | Permitir ejecutarlas con advertencia. |
| A19 | Los huecos de datos no son error: se simulan las barras existentes y se informa `data_gaps`. | Rechazar datasets con huecos. |
| A20 | Las fábricas CLI/API se duplican en `ft_vectorbt` en este ciclo y se extraen a `finances_core.backtest` cuando exista el segundo motor. | Extraerlas ya (más trabajo inicial, menos duplicación). |
| A21 *(nuevo)* | Se reutiliza la variable existente `FT_STRATEGIES_DIR` (hoy sin consumidor) como raíz de estrategias locales, con subcarpeta `<kind>/` por estrategia, resuelta a ruta absoluta respecto de la raíz del proyecto; su descripción en `ft_setup` cambia. | Crear otra variable (p. ej. `FT_BACKTEST_STRATEGIES_DIR`) y dejar la existente sin uso. |
| A22 *(nuevo)* | `strategy_code_hash` cubre solo los `*.py` de la carpeta de la estrategia; las ayudas `_lib` se cubren con `CATALOG_VERSION`. `CATALOG_VERSION` conserva su nombre pero pasa a versionar contrato e infraestructura común, no cada tipo (cada estrategia lleva `version` y `code_hash`). | Incluir `_lib` en cada hash (cualquier cambio de ayuda invalida todas las corridas) o renombrar la constante. |
| A23 *(nuevo)* | Las estrategias locales son código de confianza ejecutado en proceso, sin sandbox; la guarda AST es una barrera de buenas prácticas, no de seguridad. El límite real es que la API nunca carga código. Una estrategia local colgada (bucle infinito) bloquea su hilo: no hay *timeout* en este ciclo. | Ejecutar estrategias en un subproceso con límites (más complejidad, otro RFC). |
| A24 *(nuevo)* | El núcleo normaliza las señales (nulos y calentamiento → `false`, `entry`+`exit` simultáneas → ambas `false`) para que los motores no resuelvan ese conflicto. | Delegar la regla a cada motor (riesgo de divergencia entre motores). |
| A25 *(nuevo)* | `kinds --check` (ejecuta código de estrategia y el test de anticipación) existe solo en la CLI, como herramienta de autor local; la API solo expone `GET /kinds`. Es una excepción acotada a la simetría CLI/API del Art. 1, que se justifica por RF-18. | Exponerlo también por API (aumenta la superficie de ejecución de código). |
| A26 *(nuevo)* | La guía de autor vive en `docs/guides/strategy-authoring.md` (nueva carpeta `docs/guides/`): `docs/` ya separa decisiones (`rfc/`), gobernanza (`constitution.md`) y referencias (`articles/`); una guía de procedimiento para autores es una categoría distinta, y el README de cada estrategia documenta solo esa estrategia. El boilerplate vive junto al código en `strategies/_template/` para que no se desincronice. | Poner la guía bajo `specs/` (mezcla requisitos con procedimiento) o en el README raíz (crece sin control). |
| A27 *(nuevo)* | Se mantiene la numeración de RF-1…RF-15 y se añaden RF-16…RF-22 al final, aunque RF-16…RF-22 conceptualmente amplían RF-1/RF-2. | Renumerar y corregir todas las referencias de `specs/ft_vectorbt/`. |
