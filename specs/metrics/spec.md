# Spec — Catálogo de métricas (`metrics`) — Slice 1

## Contexto e Historias de Usuario

Capacidad **transversal** de cálculo de métricas, **independiente del consumidor**. Vive en un
hogar neutral `finances_core.metrics` (no bajo `backtest`), pensado como **paraguas** para
métricas de **trading** hoy y de **inversión** a futuro, de modo que no quede atada a un solo
dominio ni a un solo productor de datos.

La consumen por igual:
- un **backtester** (produce el ledger de trades + la curva de equity por barra),
- un **bot de trading** (vivo / paper / simulado) que arma esos datos mientras opera,
- un **analizador de trades históricos** (reconstruye ledger/equity desde un export).

Las métricas se calculan **puramente** desde un **contrato neutral**: un **ledger de operaciones**
y una **curva de equity**, con expresiones Polars sobre `Float32`. Reutilizan convenciones de
`metrics.compute` (tasa libre de riesgo 0, mercado 24/7, precisión 1e-3) pero **la anualización
entra por parámetro** (`periods_per_year: float`), no por un `timeframe` de backtest, para
servir también a datos en vivo o irregulares.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant | Como quant, **quiero** un catálogo de métricas de trading calculado desde un ledger+equity neutral **para** usarlo desde el backtester, un bot o un analizador sin reimplementarlo. |
| US-2 | Optimizador (Slice 2) | Como optimizador, **necesito** seleccionar una métrica por **nombre** con su **dirección** y **valor degenerado** **para** que un trial inválido nunca gane. |
| US-3 | Analizador | Como analizador con **solo un log de trades**, **quiero** obtener al menos las métricas de ledger (sin curva de equity) **para** evaluar operaciones históricas. |
| US-4 | Auditor | Como auditor, **exijo** fórmulas documentadas y verificadas contra una referencia de mayor precisión **para** reproducirlas. |

---

## Decisiones de alcance (cerradas con el usuario)

- **Hogar neutral `finances_core.metrics`** (paraguas trading + futura inversión), **no** bajo `backtest` ni bajo `trading`.
- **(i) Promover** el contrato canónico (schemas de ledger+equity) a `finances_core.metrics`, con **re-export** desde `finances_core.backtest.frames` para no romper a los consumidores actuales (una sola fuente de verdad).
- **Anualización por parámetro** (`periods_per_year: float`), no por `timeframe`.
- **Separación ledger vs equity**: hay métricas que solo necesitan el ledger de trades y otras que necesitan la curva de equity.
- **Treynor FUERA** (necesita benchmark/Beta); **trade_count = constraint**; **z_score = diagnóstico**.
- **NO** se cambia el `RunSummary` persistido ni el comportamiento de `backtest.metrics.compute`.

---

## Requisitos Funcionales (EARS)

### RF-1 — Contrato neutral de entrada (Ubicuo)
**EL SISTEMA DEBE** definir, en `finances_core.metrics`, el contrato canónico **ledger de trades**
y **curva de equity** (schemas de columnas y validación), independiente del motor de backtest.
`finances_core.backtest.frames` **DEBE** re-exportar esos símbolos sin cambiar su API, de modo
que los consumidores actuales (motor, runner, results_store, `backtest.metrics`) sigan igual.

### RF-2 — Cálculo puro (Ubicuo, Art. 8)
**EL SISTEMA DEBE** calcular las métricas con funciones **puras** sobre el contrato neutral, en
`Float32` con Polars, sin leer disco, sin persistir y sin depender de `finances_core.backtest`.

### RF-3 — Separación ledger / equity (Ubicuo)
**EL SISTEMA DEBE** ofrecer:
- métricas **de ledger** (solo requieren el ledger de trades): `net_profit`, `profit_factor`,
  `win_rate`, `expectancy`, `avg_win_loss_ratio`, `sqn`, diagnósticos `z_score`, `trade_count`;
- métricas **de serie temporal** (requieren la curva de equity): `sharpe`, `sortino`, familia de
  drawdown (`max_drawdown_pct`/`_abs`, `avg_drawdown`, `max_drawdown_duration_ms`), `calmar`,
  `recovery_factor`, `ulcer_index`, `martin_ratio`, `r_squared`, `annualized_recovery_factor`;
- una función combinada `compute_trading_metrics(trades, equity, *, initial_cash, periods_per_year)`.
**SI** un consumidor solo tiene ledger **ENTONCES** puede obtener las métricas de ledger sin equity.

### RF-4 — Anualización por parámetro (Ubicuo)
**EL SISTEMA DEBE** recibir `periods_per_year: float` para anualizar (Sharpe/Sortino/Calmar/Martin/
annualized recovery). El productor lo calcula (el backtester con su `periods_per_year(tf)`; un bot
según su frecuencia). El módulo **NO DEBE** depender de un `timeframe` de backtest.

### RF-5 — Dirección y valor degenerado (Evento-Driven)
Cada métrica optimizable **DEBE** tener dirección (`max`/`min`). **SI** el resultado es inválido
(sin operaciones, denominador 0, no finito) **ENTONCES** la métrica pura devuelve `None` y el
**valor-objetivo** devuelve el degenerado (`−inf` si `max`, `+inf` si `min`).

### RF-6 — Registro de objetivos (Ubicuo)
**EL SISTEMA DEBE** exponer `OBJECTIVES` (nombre → extracción, dirección, degenerado) **solo** con
métricas optimizables; `z_score` y `trade_count` **NO DEBEN** figurar como objetivos.

### RF-7 — Precisión y determinismo (RFC-0004, Art. 12)
Verificación contra referencia de mayor precisión (1e-3 relativo); mismas entradas → mismas salidas.

---

## Casos límite

| Caso | Decisión |
|------|----------|
| Solo ledger, sin equity | Devuelve métricas de ledger; las de equity no se calculan |
| Sin operaciones / sin pérdidas / DD=0 | Métrica = `None` → objetivo degenerado |
| Equity ≤ 0 | Coherente con `metrics.compute` |
| Treynor / benchmark / Beta | Fuera de alcance |
| Métricas de inversión (no trading) | Fuera de alcance de este slice, pero el hogar `finances_core.metrics` las admite a futuro |
| Cambiar `RunSummary` persistido | Fuera de alcance |
