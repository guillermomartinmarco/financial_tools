# Spec — Catálogo de métricas de trading (`backtest_metrics`) — Slice 1

## Contexto e Historias de Usuario

Este slice **amplía el catálogo de métricas** del backtesting más allá de las de `RunSummary`
(RF-7 de `finances_backtester`), con una familia de métricas de trading de uso estándar, para
poder **usarlas como objetivos de optimización** (Slice 2, `ft_vectorbt_optimize`) y, a futuro,
para reportarlas.

Las métricas se calculan **puramente** desde las tablas canónicas `trades` y `equity`
(`frames.TRADES_SCHEMA`/`EQUITY_SCHEMA`, RF-6 de `finances_backtester`), con expresiones
Polars sobre `Float32`, **sin tocar el motor** y **sin cambiar el esquema persistido**
`RunSummary` (decisión (b): el optimizador las consume en memoria; surtir `RunSummary` queda
para un slice futuro). Conviven con `metrics.compute` existente, reutilizando sus convenciones
(`periods_per_year`, tasa libre de riesgo 0, mercado 24/7, precisión 1e-3 contra una
referencia de mayor precisión).

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant | Como quant, **quiero** un catálogo de métricas de trading (rendimiento, riesgo, ajustadas al riesgo, consistencia) calculadas desde `trades`/`equity` **para** comparar y optimizar estrategias. |
| US-2 | Optimizador (Slice 2) | Como optimizador, **necesito** seleccionar una métrica por **nombre** con su **dirección** (max/min) y un **valor degenerado** ante resultados inválidos **para** que un trial degenerado nunca "gane". |
| US-3 | Auditor | Como auditor, **exijo** que cada métrica tenga una fórmula documentada y verificada contra una referencia de mayor precisión **para** reproducirla. |

---

## Decisiones de alcance (cerradas con el usuario)

- **Treynor Ratio: FUERA.** Requiere Beta contra un benchmark de mercado; un backtest
  mono-activo no tiene benchmark. Se difiere a un slice con benchmark definido.
- **Trade Count: NO es objetivo, es *constraint*.** Se expone como diagnóstico y el optimizador
  (Slice 2) lo usa como mínimo (`--min-trades`).
- **Z-Score: diagnóstico, no objetivo.** Se calcula y reporta, pero no se ofrece como objetivo
  de optimización directa.

---

## Requisitos Funcionales (EARS)

Los términos (`trades`, `equity`, `pnl`, `return`, `drawdown`, `initial_cash`, `timeframe`) son
los de `finances_backtester` RF-6/RF-7.

### RF-1 — Cálculo puro desde tablas canónicas (Ubicuo)
**EL SISTEMA DEBE** calcular todas las métricas con funciones **puras** sobre `trades`
(`TRADES_SCHEMA`) y `equity` (`EQUITY_SCHEMA`) ya validadas, en `Float32` con Polars,
**sin** leer disco, **sin** persistir y **sin** modificar `RunSummary` ni el esquema de
resultados. Reusa `metrics.periods_per_year`, tasa libre de riesgo 0 y año 24/7 (A8).

### RF-2 — Catálogo de métricas (Ubicuo)
**EL SISTEMA DEBE** proveer, como mínimo, estas métricas con su fórmula y **dirección**:

**Rendimiento (Retorno):**
- `net_profit` = `final_equity − initial_cash` (max).
- `profit_factor` = beneficio bruto / pérdida bruta (max).
- `win_rate` = ganadoras / total (max; informativa).
- `expectancy` (operación promedio) = `net_profit / n_trades` (max).
- `avg_win_loss_ratio` = ganancia promedio / pérdida promedio (max).

**Riesgo y Drawdown:**
- `max_drawdown_pct` y `max_drawdown_abs` (min).
- `avg_drawdown` (min).
- `max_drawdown_duration_ms` (min).

**Ajustadas al riesgo:**
- `sharpe`, `sortino` (max; reusan `metrics.compute`).
- `calmar` = retorno anual / |max_drawdown| (max).
- `recovery_factor` = `net_profit` / |`max_drawdown_abs`| (max).
- `ulcer_index` (min) y `martin_ratio` = retorno anual / `ulcer_index` (max).
- `sqn` (System Quality Number) = `√n_trades · media(return_trade) / desvío(return_trade)` (max).

**Consistencia y eficiencia:**
- `r_squared` de la curva de equity (max).
- `annualized_recovery_factor` (max).

**Diagnósticos (no objetivos):**
- `z_score` (dependencia de rachas), `trade_count`.

### RF-3 — Dirección y valor degenerado (Evento-Driven)
**EL SISTEMA DEBE** asociar a cada métrica una **dirección** (`max`/`min`) y, **SI** el
resultado es inválido (sin operaciones, denominador 0, `NaN`/no finito) **ENTONCES** devolver un
**valor degenerado** (`−inf` para objetivos `max`, `+inf` para `min`) de modo que nunca sea
óptimo. Las métricas puras pueden devolver `None`; el **valor-objetivo** (para optimización)
mapea `None`/no-finito al degenerado.

### RF-4 — Registro de objetivos seleccionables (Ubicuo)
**EL SISTEMA DEBE** exponer un **registro** (`OBJECTIVES`) que, dado el **nombre** canónico de
una métrica optimizable, provea `(cómo extraerla, dirección, degenerado)`. Los diagnósticos
(`z_score`, `trade_count`) **NO DEBEN** figurar como objetivos.

### RF-5 — Precisión verificada (Ubicuo, RFC-0004)
**EL SISTEMA DEBE** verificar las acumulaciones contra una referencia de mayor precisión
(tolerancia 1e-3 relativa, como `metrics.compute`), con casos de 5–10 operaciones calculados a
mano.

### RF-6 — Determinismo (Ubicuo, Art. 12)
Mismas `trades`/`equity` **DEBEN** producir las mismas métricas, en el mismo equipo y entre
corridas.

---

## Casos límite

| Caso | Decisión |
|------|----------|
| Sin operaciones (`n_trades = 0`) | `profit_factor`/`expectancy`/`win_rate`/`sqn`/`avg_win_loss` = `None` → degenerado como objetivo |
| Sin pérdidas (pérdida bruta = 0) | `profit_factor` = `None` (degenerado); documentado (no `+inf` crudo) |
| `max_drawdown = 0` | `calmar`/`recovery_factor`/`martin_ratio` = `None` (degenerado) |
| Equity ≤ 0 | coherente con `metrics.compute` (`cagr = −1`) |
| Benchmark / Beta (Treynor) | **Fuera de alcance** |
| Cambiar `RunSummary` persistido | **Fuera de alcance** en este slice |
