# Tasks — Catálogo de métricas (`metrics`) — Slice 1

> **Convención**: cada tarea es atómica, testeable y sigue TDD (Art. 7). **Hecho cuando**: criterio verificable.
> Checks: `ruff check`, `ruff format --check`, `mypy --strict src`, `pytest -q` en `packages/finances-core` **y** regresión en `packages/finances-backtester` (por la promoción de schemas).
> Tests en `packages/finances-core/tests/unit/`.
> Prerrequisitos: `finances_core.backtest.frames`/`metrics` (en `main`).
> **No** se cambia `RunSummary`, el esquema de resultados, ni el comportamiento de `backtest.metrics.compute`.
>
> **Estado**: el módulo `finances_core/metrics/{frames,trading,__init__}.py` está implementado y mergeado en `main` (`TRADES_SCHEMA`/`EQUITY_SCHEMA`, `compute_ledger_metrics`/`compute_equity_metrics`/`compute_trading_metrics`, `OBJECTIVES`/`objective_value`; re-export en `backtest/frames.py`). Con evidencia de tests (`test_trading_metrics.py`, 33 casos; + `test_metrics_frames_reexport.py` para `M0.2`): `M0.1`–`M3.3` cubiertas — hand-computed, degenerados (0 ops, sin ganancias, `DD=0`, 1 barra, equity plana, N<2), r² de recta perfecta, registro de objetivos parametrizado, **paridad** con `backtest.metrics.compute`/`RunSummary`, **precisión** vs referencia `math.fsum` y **determinismo**. **Queda abierta** `M3.4` (matriz Art. 20 del PR, artefacto de proceso).

## Fase M0: Promoción del contrato neutral (decisión (i), RF-1)

- [x] **M0.1**: Crear `finances_core/metrics/frames.py` con el contrato canónico (ledger `TRADES_SCHEMA`, equity `EQUITY_SCHEMA`, dominios, validación) **movido** desde `backtest/frames.py`
  - **Hecho cuando**: los símbolos existen en `finances_core.metrics.frames` con idénticos nombres/firmas; `mypy --strict` verde.
- [x] **M0.2**: Convertir `backtest/frames.py` en **re-export** de `finances_core.metrics.frames` — depende de `M0.1`
  - **Hecho cuando**: `finances_core.backtest.frames` expone exactamente los mismos símbolos (`__all__` igual); `test_metrics_frames_reexport.py` lo verifica; **toda** la suite de `finances-core` y de `finances-backtester` (incl. `ft_vectorbt` e `isolation`) sigue **verde** sin cambios en otros imports.

## Fase M1: Métricas de ledger (RF-3)

- [x] **M1.1**: `compute_ledger_metrics` — `net_profit`, `profit_factor`, `win_rate`, `expectancy`, `avg_win_loss_ratio`, `sqn` — depende de `M0.1`
  - **Hecho cuando**: casos a mano (con/sin pérdidas, con/sin ganancias, 0 operaciones) dan los valores esperados; indefinidos → `None`.
  - **Evidencia**: `test_ledger_metrics_hand_computed` (mixto), `test_ledger_degenerates` (0 ops, sin pérdidas) y `test_ledger_all_losses` (sin ganancias → `profit_factor` 0, ratio `None`).
- [x] **M1.2**: diagnósticos de ledger `z_score` (rachas) y `trade_count` — depende de `M1.1`
  - **Hecho cuando**: `z_score` coincide con un caso a mano, `None` con `N<2`; `trade_count == n_trades`; **no** figuran en `OBJECTIVES`.
  - **Evidencia**: `test_ledger_metrics_hand_computed` (`z_score`/`trade_count` a mano), `test_z_score_none_below_two_observations` (N<2 y clase única → `None`) y `test_objectives_registry_excludes_diagnostics`.

## Fase M2: Métricas de equity (RF-3, RF-4)

- [x] **M2.1**: `compute_equity_metrics` — `sharpe`, `sortino`, familia de drawdown (`max_drawdown_pct`/`_abs`, `avg_drawdown`, `max_drawdown_duration_ms`) — depende de `M0.1`
  - **Hecho cuando**: con una curva a mano (2 drawdowns, uno sin recuperar) los valores coinciden; `sharpe`/`sortino` coinciden con `backtest.metrics.compute` pasándole el mismo `periods_per_year`; `max_drawdown_pct` == `RunSummary.max_drawdown`.
  - **Evidencia**: `test_equity_metrics_hand_computed` (curva a mano) y `test_equity_metrics_match_backtest_run_summary` (`sharpe`/`sortino`/`max_drawdown_pct` == `RunSummary` vía `compute`+`periods_per_year("1h")`).
- [x] **M2.2**: `calmar`, `recovery_factor`, `ulcer_index`, `martin_ratio`, `r_squared`, `annualized_recovery_factor` (anualización con `periods_per_year`) — depende de `M2.1`
  - **Hecho cuando**: casos a mano verifican cada fórmula; degenerados (`DD=0`, `<2` barras, varianza 0) → `None`; `r_squared` de una recta perfecta = 1,0 (±1e-6).
  - **Evidencia**: `test_equity_ratios_hand_computed` (`calmar`/`martin_ratio`/`recovery_factor`/`annualized_recovery_factor` a mano con `periods_per_year=n`), `test_equity_degenerates_return_none` (`DD=0`, 1 barra, equity plana → `None`) y `test_r_squared_perfect_line`.

## Fase M3: Combinada, registro y cierre (RF-3, RF-5, RF-6, RF-7)

- [x] **M3.1**: `compute_trading_metrics(trades, equity|None, …)` combinada (solo-ledger si `equity=None`) — depende de `M1.1`, `M2.2`
  - **Hecho cuando**: con `equity=None` devuelve las métricas de ledger y `None` en las de equity; con equity, todas; `TradingMetrics` valida (Pydantic `extra="forbid"`).
- [x] **M3.2**: `Direction`, `OBJECTIVES`, `objective_value` + re-exports en `metrics/__init__.py` — depende de `M3.1`
  - **Hecho cuando**: `OBJECTIVES` contiene exactamente las optimizables (sin `z_score`/`trade_count`) con dirección correcta; `objective_value` → número o degenerado (`−inf`/`+inf`); test parametrizado sobre todo el registro; `from finances_core.metrics import ...` expone la API pública.
  - **Evidencia**: `test_objectives_registry_excludes_diagnostics`, `test_objective_value_defined_for_every_objective` (parametrizado sobre `OBJECTIVES`, degenerado coherente con la dirección), `test_objective_value_degenerates_by_direction` y `test_objective_value_unknown_name_raises`.
- [x] **M3.3**: Precisión (1e-3 vs referencia) + determinismo + checks — depende de `M3.2`
  - **Hecho cuando**: acumulaciones difieren <1e-3 relativo de una referencia de mayor precisión; dos cálculos idénticos coinciden; `ruff`+`mypy --strict`+`pytest` verdes en `finances-core` **y** regresión verde en `finances-backtester`.
  - **Evidencia**: `test_ulcer_index_matches_higher_precision_reference` (RMS de drawdowns vs `math.fsum`, <1e-3 rel) y `test_trading_metrics_are_deterministic` (dos cálculos idénticos campo a campo).
- [ ] **M3.4**: Matriz Art. 20 en el PR — depende de `M3.3`
  - **Hecho cuando**: 10 dimensiones; Desempeño/Concurrencia/Despliegue = N/A justificado (módulo puro); Arquitectural = promoción con re-export sin romper consumidores.
