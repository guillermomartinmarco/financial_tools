# Spec — `finances-backtester` (contratos compartidos, MVP v1.0)

## Contexto e Historias de Usuario

`finances-backtester` es el paquete (`packages/finances-backtester`, aún no existe) que alojará los motores de backtesting de la suite. Cada motor es una herramienta `ft_*` que ejecuta **la misma estrategia** sobre **los mismos datos** con una biblioteca distinta. Para que los resultados sean comparables, este documento fija lo que es común a todos los motores: el **catálogo de estrategias**, la **semántica exacta de las señales**, el **modelo de ejecución y de costes**, el **formato de resultados**, las **métricas**, la **identidad de una corrida** y el **contrato de validación cruzada**. Los contratos viven en `finances-core` (Art. 4); cada motor es un adaptador delgado.

La estrategia no es código del usuario: es un **tipo (`kind`) del catálogo** con parámetros. `ft_strategy_manager` ya versiona parámetros con hash SHA-256 (`specs/ft_strategy_manager` RF-1/RF-2); el backtester interpreta esos `parameters` como `{"kind": ..., "params": {...}}`.

El primer motor es `ft_vectorbt` (`specs/ft_vectorbt/`). Los demás (hoja de ruta al final) no se redactan ahora.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador, **quiero** ejecutar una estrategia versionada sobre un dataset y un rango **para que** obtenga operaciones, curva de capital y métricas reproducibles. |
| US-2 | Backtest Engineer | Como ingeniero de backtesting, **exijo** que cada motor produzca las mismas operaciones y la misma curva de capital (dentro de una tolerancia documentada) para el mismo tipo de estrategia **para que** las diferencias delaten defectos del adaptador y no del modelo. |
| US-3 | Quant Researcher | Como investigador, **necesito** que relanzar la misma corrida devuelva el resultado ya calculado **para que** no pierda tiempo ni genere duplicados (Art. 12). |
| US-4 | Auditor / Compliance | Como auditor, **exijo** que cada resultado registre la estrategia exacta (id, versión, hash), el dataset, los costes, el motor y su versión **para que** pueda reproducirlo. |
| US-5 | DevOps / Operador | Como operador, **quiero** que todo motor se despache con el mismo contrato asíncrono y se lea con el mismo formato **para que** la UI y los pipelines no dependan del motor. |

---

## Requisitos Funcionales (EARS)

Un **dataset** se identifica como en `ft_data_viewer`: `exchange` / `market_type` / `symbol` / `timeframe` (`DatasetIdentifier`). Una **barra** es una vela; `t` es su índice (0-based) dentro del rango cargado. Las posiciones son **solo largas** (largo o plano).

### RF-1 — Catálogo de Tipos de Estrategia (Ubícuo)
**EL SISTEMA DEBE** mantener en `finances_core.backtest.catalog` un registro de tipos (`kind`), cada uno con un modelo Pydantic de parámetros (`extra="forbid"`) y una `CATALOG_VERSION` (SemVer) que sube ante cualquier cambio de semántica de un tipo.
- **Contrato de `parameters`** (el campo guardado en `ft_strategy_manager`): exactamente `{"kind": "<tipo>", "params": {...}}`; no se admiten otras claves de primer nivel.
- **Tipos iniciales (v1.0.0)**: `sma_cross`, `rsi_reversion`, `donchian_breakout` (RF-2).
- **SI** `kind` no está en el catálogo, **ENTONCES** error de validación `unknown_kind` (exit 2 / HTTP 400) que lista los tipos válidos. No existen *plugins*, código del usuario ni un DSL de reglas.
- **SI** `params` incumple su modelo (tipo, rango, relación entre parámetros), **ENTONCES** `invalid_params` (exit 2 / HTTP 400) con el detalle por campo.
- Cada motor declara los `kind` que implementa; **SI** se pide un tipo del catálogo que el motor no implementa, **ENTONCES** `kind_not_supported_by_engine` (exit 2 / HTTP 400).
- El catálogo es consultable (`kinds` en CLI / `GET /kinds` en API) con modelo de parámetros (JSON Schema) y descripción por tipo.

### RF-2 — Semántica Exacta de las Señales (Ubícuo)
Las señales se evalúan **al cierre** de la barra `t` y producen `entry[t]`/`exit[t]` booleanos. Toda la semántica usa solo datos de las barras `≤ t`. **Calentamiento**: mientras algún dato necesario no esté disponible, la señal es `false` (sin señal), nunca un valor por defecto. Las comparaciones son estrictas (`>`/`<`); los empates numéricos no generan señal.

**`sma_cross`** — parámetros `fast`, `slow` (enteros, `2 ≤ fast < slow ≤ 10000`):
- `SMA_n(t)` = media aritmética de `close[t-n+1..t]`, definida para `t ≥ n-1`.
- Disponible (`t ≥ slow`, pues exige `t` y `t-1`): `entry[t]` = `SMA_fast(t-1) ≤ SMA_slow(t-1)` **y** `SMA_fast(t) > SMA_slow(t)`; `exit[t]` = `SMA_fast(t-1) ≥ SMA_slow(t-1)` **y** `SMA_fast(t) < SMA_slow(t)`.

**`rsi_reversion`** — parámetros `period` (entero, `2 ≤ period ≤ 1000`), `lower`, `upper` (reales, `0 < lower < upper < 100`):
- RSI de Wilder: `Δ[t] = close[t] - close[t-1]`; `ganancia = max(Δ,0)`, `pérdida = max(-Δ,0)`. Semilla en `t = period`: media simple de las primeras `period` ganancias/pérdidas (`Δ[1..period]`); después `media[t] = (media[t-1]·(period-1) + valor[t]) / period`. `RSI = 100 - 100/(1+RS)`, `RS = media_ganancia/media_pérdida`; **SI** la media de pérdidas es 0 y la de ganancias > 0, **ENTONCES** `RSI = 100`; **SI** ambas son 0, **ENTONCES** `RSI = 50` (consistente con `ft_indicators`). Definido para `t ≥ period`.
- Disponible (`t ≥ period + 1`): `entry[t]` = `RSI[t-1] ≥ lower` **y** `RSI[t] < lower`; `exit[t]` = `RSI[t-1] ≤ upper` **y** `RSI[t] > upper` (reversión a la media: se compra la sobreventa y se vende la sobrecompra).

**`donchian_breakout`** — parámetro `window` (entero, `2 ≤ window ≤ 10000`):
- `alto(t)` = `max(high[t-window..t-1])` y `bajo(t)` = `min(low[t-window..t-1])` (**excluyen** la barra actual), definidos para `t ≥ window`.
- Disponible (`t ≥ window`): `entry[t]` = `close[t] > alto(t)`; `exit[t]` = `close[t] < bajo(t)`.

**Reglas comunes de aplicación** (RF-5): con posición plana, `entry[t]` abre; con posición larga, `exit[t]` cierra; una `entry` con posición larga y una `exit` con posición plana se ignoran; **SI** `entry[t]` y `exit[t]` son verdaderas a la vez, **ENTONCES** no se actúa (por construcción no ocurre en los tipos v1.0.0).

### RF-3 — Petición de Backtest (Ubícuo)
**EL SISTEMA DEBE** aceptar una única forma canónica `BacktestRequest` (`finances_core.schemas.backtest`) con dos adaptadores equivalentes (CLI y API):
- `dataset` (`exchange`, `market_type`, `symbol`, `timeframe`); `strategy_id` (UUID v7) y `strategy_version` (opcional; **por defecto la última**, como `ft_strategy_manager` RF-2).
- `start_ms`/`end_ms` (`int64`, opcionales; rango `[start_ms, end_ms]` sobre `open_time_ms`, ambos inclusivos; por defecto todo el dataset). `start_ms ≤ end_ms`.
- `initial_cash` (`> 0`, por defecto 10 000), `size_fraction` (`0 < f ≤ 1`, por defecto 1: toda la caja), `fees_bps` (`≥ 0`, por defecto 10), `slippage_bps` (`≥ 0`, por defecto 0).
- `force` (booleano, por defecto `false`; RF-8).
- **Resolución de la estrategia**: la herramienta lee la estrategia mediante `finances_core.backtest.strategy_source` (envoltorio sobre el servicio de `ft_strategy_manager`; ver ADR-B02) y valida `parameters` con RF-1. **SI** `strategy_id` o la versión no existen, **ENTONCES** exit 1 / HTTP 404. **SI** la estrategia está *soft-deleted*, **ENTONCES** se trata como inexistente (exit 1 / 404), coherente con `ft_strategy_manager` Deriva 9.
- Fuera del MVP: varios datasets o varias estrategias por corrida (`datasets`/`strategies` listas) se rechazan por validación.

### RF-4 — Acceso a Datos (Ubícuo)
**EL SISTEMA DEBE** cargar las velas con `finances_core.backtest.candles.read_candles(settings, dataset, start_ms, end_ms)`, que reutiliza el lector y la validación compartidos de `finances_core.indicators_store.source` (`load_source`, `validate_source`; `ft_data_viewer` no es importable, Art. 4) sobre `FT_PARQUET_DIR`, en **solo lectura**.
- Columnas: `open_time_ms` (`Int64`), `open`, `high`, `low`, `close` (`Float32`); el volumen no se usa en v1.0.0.
- Validación (Art. 13): orden estrictamente creciente y único, precios finitos y `> 0`. **SI** hay nulos o no finitos en columnas de precio, **ENTONCES** `invalid_source` (exit 2 / HTTP 422); nunca se descartan filas en silencio.
- **SI** el dataset no existe, **ENTONCES** exit 1 / HTTP 404; **SI** el rango no contiene velas, **ENTONCES** `empty_range` (exit 2 / HTTP 400).
- Los huecos (gaps) de datos **no** son un error: las barras consecutivas se simulan tal cual existen en el dataset; la corrida informa `data_gaps` (cantidad de intervalos `> 1,5 × timeframe`) en el manifiesto.
- **SI** hay menos barras que el calentamiento del tipo más 2, **ENTONCES** la corrida es válida con 0 operaciones y advertencia `insufficient_history`.

### RF-5 — Modelo de Ejecución y Costes (Ubícuo)
**EL SISTEMA DEBE** simular con las reglas siguientes (idénticas en todos los motores):
1. **Sin anticipación**: la señal de la barra `t` (cierre) se ejecuta al **precio de apertura de la barra `t+1`**. Una señal en la última barra no se ejecuta. Una entrada que se ejecutaría en la última barra se ignora (no abre posiciones de cero duración).
2. **Órdenes**: solo de mercado, de tamaño completo (entrada = comprar; salida = vender toda la posición). Sin cortos, apalancamiento, parciales ni intrabarra.
3. **Precio de ejecución**: compra `open·(1 + s)`, venta `open·(1 - s)`, con `s = slippage_bps / 10 000`.
4. **Comisiones**: `fees = fee_rate · (precio_ejecución · cantidad)` en cada ejecución, con `fee_rate = fees_bps / 10 000`, descontadas de la caja.
5. **Tamaño**: `cantidad = size_fraction · caja / (precio_ejecución · (1 + fee_rate))` (cantidades fraccionarias; la caja cubre las comisiones). Con `size_fraction = 1` la caja queda en 0 tras entrar.
6. **Liquidación final**: **SI** hay posición larga al terminar el rango, **ENTONCES** se cierra al **precio de cierre de la última barra** (con el mismo coste de venta), `exit_reason = 1` (`end_of_data`).
7. **Capital**: `equity[t] = caja[t] + cantidad[t] · close[t]` al cierre de cada barra (valoración a mercado).

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

### RF-8 — Identidad e Idempotencia (Ubícuo, Art. 12)
**EL SISTEMA DEBE** calcular `run_id` como SHA-256 (hex) del JSON canónico (claves ordenadas, sin espacios) de: `strategy_hash` (de `ft_strategy_manager`), `strategy_id` y versión resueltas, dataset, `start_ms`/`end_ms` **efectivos**, `data_hash` (SHA-256 del contenido de las columnas de velas del rango, ver A10), `initial_cash`, `size_fraction`, `fees_bps`, `slippage_bps`, `engine` (nombre), `engine_version` (versión de la biblioteca), `CATALOG_VERSION` y `RESULT_SCHEMA_VERSION`.
- **SI** ya existe un resultado completo con ese `run_id`, **ENTONCES** se devuelve sin recalcular (`cached = true`), salvo `force = true`.
- Con `force = true` se recalcula y se reemplaza de forma atómica; el contenido de `trades.parquet`, `equity.parquet` y `summary.json` DEBE ser idéntico al anterior (determinismo, RF-5 de `ft_vectorbt`).
- Dos tareas concurrentes con el mismo `run_id` son seguras: cada una escribe en un directorio temporal propio y la que termina segunda descarta el suyo y devuelve el existente.

### RF-9 — Almacén de Resultados (Ubícuo)
**EL SISTEMA DEBE** persistir cada corrida en
`{FT_RESULTS_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/{run_id}/` (ver A11) con `trades.parquet`, `equity.parquet`, `summary.json` y `manifest.json`.
- `manifest.json` es el **marcador de completitud** y se escribe el último: petición resuelta, `strategy_id`/`version`/`hash`/`kind`/`params`, dataset, rango efectivo, `data_hash`, costes, `engine`/`engine_version`, `CATALOG_VERSION`, `RESULT_SCHEMA_VERSION`, `created_at_ms`, `duration_ms`, versión de Python y de las bibliotecas relevantes, advertencias.
- Escritura atómica: se escribe en `{run_id}.tmp-{uuid}` hermano y se renombra; un directorio sin `manifest.json` no se considera resultado y se ignora (y se limpia al recalcular). Parquet con compresión fija (`zstd`) y sin metadatos dependientes del tiempo, para reproducibilidad byte a byte.
- Lectura: listado por filtros (`exchange`, `market_type`, `symbol`, `timeframe`, `strategy_id`, `engine`) con paginación por cursor; lectura de `trades`/`equity` paginada por `entry_time_ms`/`open_time_ms` (`after_ms`, `limit` 1..10000).
- `FT_RESULTS_DIR` es compartido por todos los motores y por otros resultados de la suite; el almacén solo reconoce directorios con `manifest.json` válido.

### RF-10 — Contrato de Motor y Validación Cruzada (Ubícuo)
**EL SISTEMA DEBE** definir en `finances_core.backtest.engine_api` el contrato `BacktestEngine`: `name`, `version() -> str`, `supported_kinds`, y `run(candles, strategy, costs, sizing) -> EngineOutput` (`trades` y `equity` canónicos, RF-6) más `validate_engine_output`.
- **Orquestación común**: `finances_core.backtest.runner.run_backtest(request, engine, settings, on_phase)` ejecuta resolver → cargar → identidad → ¿existe? → motor → validar → métricas → persistir, de modo que `service.py` de cada motor solo aporta el motor.
- **Contrato de validación cruzada**: todo motor que implemente un `kind` **DEBE** pasar la suite de contrato `tests/contract/` del paquete sobre un **dataset de fixture determinista** (semilla fija, ≥ 2 000 barras, con tendencia y rango y sin empates numéricos en los umbrales) frente a *golden files* (señales, operaciones, equity y métricas) generados por una referencia independiente **de prueba** (fuera de `src/`). Criterios: mismas operaciones (`n_trades`, `entry_time_ms`, `exit_time_ms`, `side`, `exit_reason` exactos) y equity/precios/pnl/métricas dentro de la tolerancia de RFC-0004 (punto 5). Una diferencia es un defecto del adaptador.
- Con un segundo motor la misma suite compara además motor contra motor sin cambios.

### RF-11 — Frontera Numérica (Ubícuo, RFC-0004)
**EL SISTEMA DEBE** mantener `float32`/`int64` en todos los contratos, el almacén, las métricas y los módulos de servicio (Art. 5, 8). Los motores basados en `pandas`/NumPy confinan lo que necesitan al módulo `engine.py` con dos puntos de conversión (RFC-0004). Un test AST por motor y otro para `finances_core/backtest/` lo verifican.
- **SI** RFC-0004 no está aprobada (y el Art. 8 enmendado), **ENTONCES** ningún motor basado en `pandas` se fusiona.

### RF-12 — Configuración vía `FT_` Settings (Ubícuo)
**EL SISTEMA DEBE** leer de `FinancesSettings` (prefijo `FT_`): `FT_PARQUET_DIR`, `FT_RESULTS_DIR`, `FT_TASKS_DB_PATH`, `FT_STRATEGIES_DB_PATH` (o `FT_SHARE_DIR/strategies.db`) y `FT_LOG_*`. Variable **nueva**: `FT_BACKTEST_MAX_CONCURRENT` (entero 1..16, por defecto 2): corridas simultáneas por proceso; una más espera en cola. Debe registrarse en `ft_setup/env_list.conf` (ver `tasks.md`, «Cambios en otros documentos»).

### RF-13 — Logging Dual y Auditoría (Ubícuo)
**EL SISTEMA DEBE** usar `structlog` (Art. 9). Cada corrida registra un evento INFO de auditoría `backtest_run` con `task_id`, `run_id`, `engine`, `engine_version`, `strategy_id`, `strategy_version`, `strategy_hash`, `kind`, `dataset`, rango, `n_bars`, `n_trades`, `cached`, `forced`, `duration_ms` y `result` (`completed` | `cached` | `failed`). Métricas: `load_ms`, `engine_ms`, `persist_ms`. CLI: tablas Rich; API: JSON con `component="ft_<motor>"`.

### RF-14 — Errores y Códigos de Salida (Evento-Driven)
Tabla común a todos los motores (cada `ft_*` la reproduce en su spec):
| Condición | CLI exit | API |
|-----------|----------|-----|
| OK (incluye resultado en caché y 0 operaciones) | 0 | 200 / 202 |
| Dataset, estrategia/versión, corrida o tarea no encontrada | 1 | 404 |
| `unknown_kind`, `invalid_params`, `kind_not_supported_by_engine`, rango inválido o vacío, costes inválidos, `invalid_source` | 2 | 400 / 422 |
| Tarea `FAILED` (excepción inesperada del motor, `output_out_of_range`) | 3 | 500 en `/result` |
| `/result` de una tarea no terminada | — | 409 |
| Error de IO (disco, permisos) | 4 | 500 |

### RF-15 — Aislamiento (No deseado, Art. 4)
**SI** un motor importa de otro motor `ft_*`, de `finances_downloader`, `finances_analytics`, `ft_data_viewer` o `finances_api`, **ENTONCES** es una violación del Art. 4 (test AST por motor). Los motores solo importan `finances_core` y su propia biblioteca. `finances-api` importa el `router` público de cada motor (excepción del Art. 4).

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Cortos, apalancamiento, carteras multi-activo** | **Fuera de alcance MVP**. Una corrida = un dataset y una estrategia, largo/plano. `side` reserva `-1`. |
| **Ejecución intrabarra, tipos de orden distintos del mercado, órdenes parciales** | **Fuera de alcance**. |
| **Optimización de parámetros, *walk-forward*, barridos** | **Fuera de alcance**: pertenece a `ft_backtestopt_spectre` (motor GPU). |
| **Trading en vivo o *paper*** | **Fuera de alcance**: bots (`ft_bot_*`). |
| **Código de usuario, *plugins*, DSL de reglas** | **Prohibido**. Solo tipos del catálogo. |
| **Indicadores desde el almacén de `ft_indicators`** | **Fuera de alcance MVP**. Cada motor calcula sus señales desde las velas (semántica de RF-2). Una ruta opcional futura leería columnas del almacén con el lector compartido (`finances_core.indicators_store`); no entra ahora porque exigiría que el usuario precalcule series con parámetros idénticos, acopla la corrida al estado de otro almacén (cambia `run_id` y reproducibilidad) y las definiciones de RSI/SMA de `ft_indicators` deberían congelarse como contrato. |
| **Gráficos** | **Fuera de alcance**. |
| **Varias fuentes de datos fusionadas, remuestreo de timeframes** | **Fuera de alcance**. Un dataset del almacén, tal cual. |
| **Calendarios bursátiles y anualización por sesiones** | **Fuera de alcance MVP** (A8). |

## Hoja de Ruta de Motores (no se redactan ahora)

| Orden | Herramienta | Biblioteca / nota |
|---|---|---|
| 1 | `ft_vectorbt` | VectorBT — **este ciclo** (`specs/ft_vectorbt/`) |
| 2 | `ft_backtesting_py` | backtesting.py; mismo contrato, RFC-0004 ya cubre su `engine.py` |
| 3 | `ft_backtrader` | Backtrader; orientado a eventos, puede requerir adaptación de las reglas de RF-5 |
| 4 | `ft_bt` | bt (carteras); fuera de los tipos del MVP hasta que se admitan carteras |
| 5 | `ft_ffn` | ffn: métricas de rendimiento, no un simulador; se evaluará como contraste de RF-7 |
| 6 | `ft_backtestopt_spectre` | C++/CUDA, `float32` nativo, optimización; RFC propio (Art. 11, 19) |
| 7 | `ft_bot_paper_binance`, `ft_live_bot` | Bots; fuera del alcance de backtesting |

Cada motor nuevo añade su propio trío `specs/ft_<motor>/` y reutiliza este paquete de contratos y la suite de RF-10.

---

## Supuestos y Decisiones Abiertas

**Decisión tomada por el usuario (requisito, no se discute):** la estrategia es un catálogo de tipos (`kind` + `params` en `ft_strategy_manager`); solo ejecutan los tipos del catálogo; sin *plugins* ni DSL.

Todo lo siguiente es **propuesta del diseño** (marcada como supuesto) y está sujeto a veto:

| ID | Supuesto / decisión propuesta | Consecuencia si se veta |
|----|-------------------------------|--------------------------|
| A1 | Contratos compartidos en `finances_core/backtest/` (catálogo, costes, métricas, identidad, almacén, runner, contrato de motor) y `finances_core/schemas/backtest.py`. | Cada motor duplicaría el pipeline; se rompe el contrato común. |
| A2 | Los motores leen la estrategia vía `finances_core.backtest.strategy_source` (envoltorio sobre `ft_strategy_manager`). Verificado: `ft_strategy_manager` vive en `finances-core`, pero los tests AST de las demás herramientas prohíben todo import de raíz `ft_*`; el envoltorio mantiene intacta esa regla. | Habría que permitir `ft_strategy_manager` como excepción explícita en los tests AST y en el Art. 4. |
| A3 | El lector de velas se construye sobre `indicators_store.source` (`load_source`/`validate_source`) más filtro de rango. | Escribir un lector nuevo duplicaría la validación. |
| A4 | Alcance MVP: un dataset, una estrategia, largo/plano, caja inicial, fracción fija, `fees_bps`, `slippage_bps`. | — |
| A5 | Ejecución: señal al cierre de `t` → apertura de `t+1`; entradas en la última barra ignoradas; liquidación final al **cierre** de la última barra con `exit_reason = 1`. | Cambia RF-5/RF-6 y los *golden*. |
| A6 | Semántica de señales de RF-2: cruces (eventos) para `sma_cross` y `rsi_reversion`; condición (estado) para `donchian_breakout`; RSI de Wilder con semilla SMA; Donchian excluye la barra actual y usa la misma `window` para entrada y salida. | Cambia `CATALOG_VERSION` y los *golden*. |
| A7 | Valores por defecto: `initial_cash = 10 000`, `size_fraction = 1`, `fees_bps = 10`, `slippage_bps = 0`. | Solo cambia la petición por defecto. |
| A8 | Anualización con año de calendario continuo (365,25 días, mercado 24/7), tasa libre de riesgo 0; incorrecta para acciones con sesiones (sobreestima `P`). | Requiere calendario por mercado (fuera de MVP). |
| A9 | Conjunto de métricas de RF-7 (incluye `cagr`) calculado en `finances_core`, no en el motor. | Cada motor tendría sus fórmulas y no serían comparables. |
| A10 | `run_id` incluye `data_hash` (contenido de las velas del rango) y `RESULT_SCHEMA_VERSION`, además de lo pedido: así reingestar o corregir velas no devuelve un resultado obsoleto. Coste: hashear las columnas del rango en cada petición. | `run_id` más estable pero potencialmente obsoleto ante cambios de datos. |
| A11 | Ruta de resultados con `market_type=` (`{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/{run_id}/`), coherente con los demás almacenes, en lugar de `{exchange}/{symbol}/{timeframe}/{run_id}` (que mezcla `spot` y `futures` del mismo símbolo). | Volver a la ruta sin `market_type`. |
| A12 | `trades` añade `exit_reason` (`Int8`) a las columnas pedidas, para distinguir la liquidación final. | Eliminarla y tratar la liquidación final como salida normal. |
| A13 | Con un solo motor, la «validación cruzada» se hace contra *golden files* de una referencia independiente de prueba; la comparación motor-contra-motor llega con el segundo motor. | — |
| A14 | Tolerancias iniciales de RFC-0004 (punto 5), a calibrar con evidencia. | — |
| A15 | Única variable nueva: `FT_BACKTEST_MAX_CONCURRENT`. | — |
| A16 | Paquete `requires-python = ">=3.11,<3.14"` hasta que V0.2 de `ft_vectorbt` pruebe la matriz; la `.venv` local usa Python 3.14 y pandas 3.x, probablemente incompatibles con VectorBT. Versión de `vectorbt`: **pin a verificar** (no se inventa). | Ajustar la matriz de CI y la `.venv`. |
| A17 | RFC-0004 enmienda el Art. 8 (constitución 1.2.0) para admitir `pandas` en `engine.py`, porque el Art. 8 no prevé excepciones por RFC. | Sin enmienda, no hay motores basados en `pandas`. |
| A18 | Estrategias *soft-deleted* se tratan como inexistentes al ejecutar. | Permitir ejecutarlas con advertencia. |
| A19 | Los huecos de datos no son error: se simulan las barras existentes y se informa `data_gaps`. | Rechazar datasets con huecos. |
| A20 | Las fábricas CLI/API se duplican en `ft_vectorbt` en este ciclo y se extraen a `finances_core.backtest` cuando exista el segundo motor. | Extraerlas ya (más trabajo inicial, menos duplicación). |
