# Plan — Catálogo de métricas (`metrics`) — Slice 1

## 1. Estructura

```
packages/finances-core/src/finances_core/
  metrics/                      # NUEVO hogar neutral (paraguas trading + futura inversión)
    __init__.py                 # re-exporta la API pública
    frames.py                   # contrato neutral: ledger de trades + curva de equity (PROMOVIDO)
    trading.py                  # catálogo de trading + registro de objetivos
  backtest/
    frames.py                   # PASA a RE-EXPORTAR desde finances_core.metrics.frames (compat)
    metrics.py                  # SIN cambios de comportamiento (RunSummary); puede pasar periods_per_year
packages/finances-core/tests/unit/
  test_metrics_frames_reexport.py  # NUEVO: el re-export mantiene la API de backtest.frames
  test_trading_metrics.py          # NUEVO: casos a mano + precisión + degenerados + split ledger/equity
```

`finances_core.metrics` importa solo `polars` (y `pydantic` para los modelos). **No** importa
`finances_core.backtest` (la dependencia va al revés: `backtest.frames` re-exporta de `metrics`).

## 2. Promoción del contrato (decisión (i), RF-1)

Mover de `finances_core.backtest.frames` → `finances_core.metrics.frames`, **sin cambiar nombres
ni firmas**: los schemas `TRADES_SCHEMA`, `EQUITY_SCHEMA`, sus dominios (`SIDE_DOMAIN`,
`EXIT_REASON_*`) y la validación (`validate_output`/equivalente). Luego `backtest/frames.py` queda
como:

```python
from finances_core.metrics.frames import (  # re-export para compatibilidad
    EQUITY_SCHEMA, TRADES_SCHEMA, SIDE_DOMAIN, EXIT_REASON_END_OF_DATA, validate_output, ...
)
__all__ = [...]
```

Así, **ningún** import existente (`engine`, `runner`, `results_store`, `backtest.metrics`,
`ft_vectorbt.*`) cambia. Regresión: toda la suite de `finances-core` y `finances-backtester` debe
seguir verde (incl. `ft_vectorbt` tests e `isolation`).

> Nota de nombres: el contrato es neutral, pero describe un **libro de operaciones** (trades) y una
> **curva de equity**; sirve a backtester, bot y analizador por igual.

## 3. API de métricas (RF-3, RF-4)

```python
# finances_core/metrics/trading.py
class TradingMetrics(BaseModel):
    # --- ledger (solo trades) ---
    net_profit: float
    profit_factor: float | None
    win_rate: float | None
    expectancy: float | None
    avg_win_loss_ratio: float | None
    sqn: float | None
    z_score: float | None          # diagnóstico
    trade_count: int               # diagnóstico / constraint
    # --- serie temporal (equity) --- (None si se calculó solo-ledger)
    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float | None
    max_drawdown_abs: float | None
    avg_drawdown: float | None
    max_drawdown_duration_ms: int | None
    calmar: float | None
    recovery_factor: float | None
    ulcer_index: float | None
    martin_ratio: float | None
    r_squared: float | None
    annualized_recovery_factor: float | None

def compute_ledger_metrics(trades: pl.DataFrame, *, initial_cash: float) -> LedgerMetrics: ...
def compute_equity_metrics(
    equity: pl.DataFrame, *, initial_cash: float, periods_per_year: float
) -> EquityMetrics: ...
def compute_trading_metrics(
    trades: pl.DataFrame, equity: pl.DataFrame | None, *,
    initial_cash: float, periods_per_year: float,
) -> TradingMetrics:
    """Combina ledger + (si hay equity) serie temporal. equity=None => campos de equity en None."""
```

Fórmulas: iguales a las ya acordadas (profit_factor, expectancy, avg win/loss, familia de
drawdown, calmar = `cagr/|maxDD|`, recovery = `net_profit/|maxDD_abs|`, ulcer = `√media(dd²)`,
martin = `cagr/ulcer`, sqn = `√N·media(ret)/desvío(ret)`, r² lineal sobre equity, annualized
recovery = `(retorno simple anualizado)/|maxDD_pct|`, z-score = rachas Wald–Wolfowitz).
`cagr`/retorno anualizado se derivan con `periods_per_year` recibido (no con `timeframe`).
Política pow-free (solo `+ − × ÷ √`).

## 4. Registro de objetivos (RF-5, RF-6)

```python
class Direction(StrEnum): MAX="max"; MIN="min"

@dataclass(frozen=True)
class Objective:
    name: str; direction: Direction; extract: Callable[[TradingMetrics], float | None]

OBJECTIVES: Mapping[str, Objective]   # excluye z_score y trade_count
def objective_value(name: str, m: TradingMetrics) -> float:  # None/no-finito -> degenerado
```

`finances_core/metrics/__init__.py` re-exporta: `TradingMetrics`, `compute_trading_metrics`,
`compute_ledger_metrics`, `compute_equity_metrics`, `OBJECTIVES`, `objective_value`, `Direction`,
y el contrato de `frames`.

## 5. ADR
| ID | Decisión | Razón |
|----|----------|-------|
| ADR-M1 | Hogar neutral `finances_core.metrics` (no `backtest`, no `trading`) | Paraguas para trading **y** futura inversión; desacopla del productor |
| ADR-M2 | (i) Promover schemas + re-export desde `backtest.frames` | Una sola fuente del contrato; cero cambios en consumidores |
| ADR-M3 | Anualización por `periods_per_year: float` | Sirve a backtest, bot (vivo/irregular) y analizador |
| ADR-M4 | Split `compute_ledger_metrics` / `compute_equity_metrics` | Un analizador con solo ledger igual obtiene métricas |
| ADR-M5 | `None` = indefinido; degenerado solo en `objective_value` | Separa el dato puro de la política del optimizador |
| ADR-M6 | No tocar `RunSummary` ni `backtest.metrics.compute` | Cambio acotado; sin bump de esquema |

## 6. Tests (Art. 7)
- Re-export: `backtest.frames` sigue exponiendo los mismos símbolos; suites de `finances-core` y `finances-backtester` verdes (regresión).
- Casos a mano por grupo (ledger y equity); solo-ledger (`equity=None`).
- Degenerados → `None` y `objective_value` → ±inf.
- Precisión 1e-3 vs referencia; determinismo.
