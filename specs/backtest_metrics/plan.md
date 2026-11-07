# Plan — Catálogo de métricas de trading (`backtest_metrics`) — Slice 1

## 1. Estructura

```
packages/finances-core/src/finances_core/backtest/
  metrics.py          # EXISTE: compute(...) -> RunSummary (RF-7). Se AMPLÍA sin romperlo.
  trading_metrics.py  # NUEVO: catálogo de métricas de trading + registro de objetivos
packages/finances-core/tests/unit/
  test_trading_metrics.py  # NUEVO: casos a mano + precisión + degenerados
```

No se toca `frames.py`, ni `RunSummary`, ni `results_store`, ni el motor. `trading_metrics.py`
importa solo `polars`, `finances_core.backtest.frames` (schemas) y `finances_core.backtest.metrics`
(para `periods_per_year` y reusar `sharpe`/`sortino` vía `compute`).

## 2. Contrato de datos

```python
class TradingMetrics(BaseModel):            # finances_core.schemas.backtest (o dataclass local)
    """Catálogo extendido calculado desde trades+equity. Valores Float32 serializados; None = no definido."""
    # Rendimiento
    net_profit: float
    profit_factor: float | None
    win_rate: float | None
    expectancy: float | None
    avg_win_loss_ratio: float | None
    # Riesgo / Drawdown
    max_drawdown_pct: float          # <= 0 (misma convención que RunSummary.max_drawdown)
    max_drawdown_abs: float          # <= 0, en unidades de caja
    avg_drawdown: float
    max_drawdown_duration_ms: int
    # Ajustadas al riesgo
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    recovery_factor: float | None
    ulcer_index: float | None
    martin_ratio: float | None
    sqn: float | None
    # Consistencia
    r_squared: float | None
    annualized_recovery_factor: float | None
    # Diagnósticos (no objetivos)
    z_score: float | None
    trade_count: int
```

## 3. Fórmulas (cuerpos con TDD)

Sea `eq` la serie `equity.equity`, `dd` la serie `equity.drawdown` (≤ 0, en fracción), `pnl` y
`ret` las columnas `trades.pnl` / `trades.return`, `P = periods_per_year(timeframe)`,
`N = n_trades`, `peak[t] = max(eq[0..t])`.

- `net_profit = eq.last() − initial_cash`.
- `gross_profit = Σ pnl[pnl>0]`; `gross_loss = −Σ pnl[pnl<0]` (≥ 0);
  `profit_factor = gross_profit / gross_loss` (None si `gross_loss = 0` o `N = 0`).
- `win_rate = #(pnl>0) / N` (None si `N=0`).
- `expectancy = net_profit / N` (None si `N=0`).
- `avg_win = media(pnl[pnl>0])`; `avg_loss = −media(pnl[pnl<0])`;
  `avg_win_loss_ratio = avg_win / avg_loss` (None si no hay pérdidas o no hay ganancias).
- `max_drawdown_pct = min(dd)` (reusa `RunSummary.max_drawdown`).
- `abs_dd[t] = eq[t] − peak[t]`; `max_drawdown_abs = min(abs_dd)`.
- `avg_drawdown = media(dd[dd<0])` (None si nunca hubo drawdown).
- `max_drawdown_duration_ms`: mayor tramo contiguo con `eq < peak` previo, medido por
  `open_time_ms` (desde el pico hasta recuperar ≥ pico; si no recupera, hasta la última barra).
- `sharpe`, `sortino`: de `metrics.compute(trades, equity, …)` (una sola fuente).
- `cagr` (de `metrics.compute`) → `calmar = cagr / |max_drawdown_pct|` (None si DD=0 o cagr None).
- `recovery_factor = net_profit / |max_drawdown_abs|` (None si DD=0).
- `ulcer_index = √( media(dd²) )` (dd en fracción; None si no hay barras).
- `martin_ratio = cagr / ulcer_index` (None si ulcer=0/None).
- `sqn = √N · media(ret) / desvío(ret, ddof=1)` (None si N<2 o desvío=0).
- `r_squared`: R² de regresión lineal de `eq` (o `ln(eq)`; decidir y documentar: **lineal sobre eq**
  en v1) contra el índice de barra; None si <2 barras o varianza 0.
- `annualized_recovery_factor = (net_profit/initial_cash · P/n_bars) / |max_drawdown_pct|`
  (retorno anualizado simple / DD; None si DD=0). *Nota: emparentado con Calmar; se documenta la
  diferencia (numerador retorno simple anualizado vs CAGR).* 
- `z_score`: prueba de rachas (Wald–Wolfowitz) sobre la secuencia gana/pierde:
  `Z = (R − R̄)/σ_R`, `R` = nº de rachas; None si N<2. **Diagnóstico.**
- `trade_count = N`. **Diagnóstico/constraint.**

Política numérica: solo `+ − × ÷ √` sobre valores agregados (sin `pow` en base negativa,
alineado con la lección "pow-free" y A8); anualización simple, no CAGR salvo donde ya lo da
`metrics.compute`.

## 4. Registro de objetivos (RF-4)

```python
class Direction(StrEnum): MAX = "max"; MIN = "min"

@dataclass(frozen=True)
class Objective:
    name: str
    direction: Direction
    extract: Callable[[TradingMetrics], float | None]

#: Solo métricas OPTIMIZABLES (excluye z_score y trade_count).
OBJECTIVES: Mapping[str, Objective]  # net_profit, profit_factor, win_rate, expectancy,
# avg_win_loss_ratio, max_drawdown_pct(min), max_drawdown_abs(min), avg_drawdown(min),
# max_drawdown_duration_ms(min), sharpe, sortino, calmar, recovery_factor, ulcer_index(min),
# martin_ratio, sqn, r_squared, annualized_recovery_factor

def objective_value(name: str, m: TradingMetrics) -> float:
    """Valor para optimización: None/no-finito -> degenerado (-inf si MAX, +inf si MIN)."""
```

Función principal:
```python
def compute_trading_metrics(
    trades: pl.DataFrame, equity: pl.DataFrame, *, initial_cash: float, timeframe: str
) -> TradingMetrics: ...
```

## 5. Decisiones de arquitectura (ADR)
| ID | Decisión | Razón |
|----|----------|-------|
| ADR-M1 | Módulo nuevo `trading_metrics.py`, no tocar `metrics.compute`/`RunSummary` | Decisión (b): cambio chico, sin bump de esquema ni regenerar golden |
| ADR-M2 | Reusar `sharpe`/`sortino`/`cagr` de `metrics.compute` | Una sola fuente; evita divergencia |
| ADR-M3 | `None` para indefinido; el degenerado vive en `objective_value` | Separa el dato puro de la política del optimizador |
| ADR-M4 | Treynor fuera; `z_score`/`trade_count` diagnósticos | Benchmark/Beta no disponible; evitar objetivos trampa |

## 6. Tests (Art. 7)
- Casos a mano (5–10 operaciones) por grupo de métricas, con valores esperados calculados.
- Degenerados: sin trades, sin pérdidas, DD=0 → `None` y `objective_value` → ±inf correcto.
- Precisión 1e-3 contra referencia de mayor precisión (como `test_` de `metrics`).
- Determinismo: dos cálculos idénticos.
