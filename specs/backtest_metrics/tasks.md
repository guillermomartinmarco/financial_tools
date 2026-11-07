# Tasks — Catálogo de métricas de trading (`backtest_metrics`) — Slice 1

> **Convención**: cada tarea es atómica, testeable y sigue TDD (Art. 7). **Hecho cuando**: criterio verificable.
> Checks: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` en `packages/finances-core`.
> Tests en `packages/finances-core/tests/unit/test_trading_metrics.py`.
> Prerrequisitos: `finances_core.backtest.{frames, metrics}` (RF-6/RF-7, ya en `main`).
> **No** se toca `RunSummary`, el esquema de resultados, el motor ni `results_store` (decisión (b)).

## Fase M0: Contrato

- [ ] **M0.1**: Modelo `TradingMetrics` (campos del plan §2) y esqueleto `trading_metrics.py`
  - **Hecho cuando**: `from finances_core.backtest.trading_metrics import TradingMetrics, compute_trading_metrics, OBJECTIVES, objective_value, Direction` importa; `compute_trading_metrics` acepta `trades`/`equity` canónicos y devuelve `TradingMetrics`; `mypy --strict` verde.

## Fase M1: Métricas de rendimiento (RF-2)

- [ ] **M1.1**: `net_profit`, `profit_factor`, `win_rate`, `expectancy`, `avg_win_loss_ratio` — depende de `M0.1`
  - **Hecho cuando**: casos a mano (con y sin pérdidas, con y sin ganancias, 0 operaciones) dan los valores esperados; `profit_factor`/`win_rate`/`expectancy`/`avg_win_loss_ratio` = `None` cuando no están definidos.

## Fase M2: Riesgo y drawdown (RF-2)

- [ ] **M2.1**: `max_drawdown_pct`, `max_drawdown_abs`, `avg_drawdown`, `max_drawdown_duration_ms` — depende de `M0.1`
  - **Hecho cuando**: con una curva de equity calculada a mano (con 2 drawdowns, uno sin recuperar al final) los cuatro valores coinciden; `max_drawdown_pct` == `RunSummary.max_drawdown` de la misma corrida; `duration` medido por `open_time_ms`.

## Fase M3: Ajustadas al riesgo (RF-2)

- [ ] **M3.1**: `sharpe`, `sortino` reutilizando `metrics.compute` (misma fuente) — depende de `M0.1`
  - **Hecho cuando**: coinciden exactamente con `RunSummary.sharpe`/`sortino` de la misma corrida (sin reimplementar).
- [ ] **M3.2**: `calmar`, `recovery_factor`, `ulcer_index`, `martin_ratio`, `sqn` — depende de `M2.1`, `M3.1`
  - **Hecho cuando**: casos a mano verifican cada fórmula (plan §3); degenerados (`DD=0`, `N<2`, desvío 0) → `None`.

## Fase M4: Consistencia (RF-2)

- [ ] **M4.1**: `r_squared` (lineal sobre `equity` vs índice de barra) y `annualized_recovery_factor` — depende de `M2.1`
  - **Hecho cuando**: `r_squared` de una recta perfecta = 1,0 (±1e-6) y de una curva ruidosa < 1; `annualized_recovery_factor` coincide con la fórmula del plan; `None` en los degenerados.
- [ ] **M4.2**: diagnósticos `z_score` (rachas Wald–Wolfowitz) y `trade_count` — depende de `M1.1`
  - **Hecho cuando**: `z_score` coincide con un caso de rachas calculado a mano; `None` con `N<2`; `trade_count == n_trades`. **No** aparecen en `OBJECTIVES`.

## Fase M5: Registro de objetivos (RF-3, RF-4)

- [ ] **M5.1**: `Direction`, `OBJECTIVES` y `objective_value` — depende de `M1.1`–`M4.1`
  - **Hecho cuando**: `OBJECTIVES` contiene exactamente las métricas optimizables (sin `z_score`/`trade_count`) con su dirección correcta; `objective_value(name, m)` devuelve el número o el **degenerado** (`−inf` si `MAX`, `+inf` si `MIN`) ante `None`/no-finito; test parametrizado sobre todo el registro.

## Fase M6: Precisión y cierre (RF-5, RF-6, Art. 20)

- [ ] **M6.1**: Test de precisión contra referencia de mayor precisión (1e-3) y de determinismo — depende de `M5.1`
  - **Hecho cuando**: las acumulaciones (sharpe/sortino/ulcer/sqn/r²) difieren < 1e-3 relativo de una referencia `float64`/`statistics`; dos cálculos idénticos dan el mismo resultado; `ruff`+`mypy --strict`+`pytest` verdes en `finances-core`.
- [ ] **M6.2**: Matriz Art. 20 en el PR (Funcional, Contractual, Datos, Calidad, etc.) — depende de `M6.1`
  - **Hecho cuando**: checklist del PR firmado; Desempeño/Concurrencia/Despliegue = N/A justificado (módulo puro).
