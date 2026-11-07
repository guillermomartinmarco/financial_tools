# Tasks — `ft_vectorbt optimize` (optimizador de parámetros) — Slice 2

> **Convención**: cada tarea es atómica, testeable y sigue TDD (Art. 7). **Hecho cuando**: criterio verificable.
> Checks: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` en `packages/finances-backtester`; `test_isolation.py` DEBE seguir verde.
> Tests en `packages/finances-backtester/tests/ft_vectorbt/test_optimize.py` (reusa las *fixtures* de `conftest`: `FT_PROJECT_ROOT` temporal, *fixture* Hive, `strategies.db` temporal). Sin red.
> **Prerrequisitos**: **Slice 1 (`backtest_metrics`) mergeado** (`OBJECTIVES`, `objective_value`, `TradingMetrics`); `ft_vectorbt` fases 0–7 (en `main`).

## Fase O0: Dependencia y contratos

- [ ] **O0.1**: Añadir `optuna` a `packages/finances-backtester/pyproject.toml` y fijar el pin con evidencia (`pip list`) — depende de Slice 1
  - **Hecho cuando**: `pip install -e "packages/finances-backtester[dev]"` instala `optuna`; `python -c "import optuna; print(optuna.__version__)"` responde; versión y cota registradas aquí; (opcional) smoke de import en `ci.yml` análogo a vectorbt.
- [ ] **O0.2**: Modelos `SweepAxis`, `SweepSpec`, `FoldScore`, `OptimizeResult` (plan §3) en `optimize.py` — depende de `O0.1`
  - **Hecho cuando**: importan y validan con Pydantic (`extra="forbid"`); `SweepSpec` rechaza ejes vacíos y `param` duplicados; `mypy --strict` verde.

## Fase O1: Espacio de búsqueda (RF-2)

- [ ] **O1.1**: `validate_sweep(spec, kind, settings)` contra el esquema `Params` de la estrategia (vía `strategy_source`) — depende de `O0.2`
  - **Hecho cuando**: un eje inexistente, un rango inválido (`low>=high`, `step<=0`) o `choices` vacío lanzan `InvalidSweepError`; un sweep válido de `sma_cross` (`fast`,`slow`) pasa; **no** importa `catalog`/`strategies` (aislamiento).

## Fase O2: Walk-forward anclado (RF-4)

- [ ] **O2.1**: `anchored_folds(n_bars, n_folds, in_sample_ratio)` + traducción a rangos `start_ms/end_ms` — depende de `O0.2`
  - **Hecho cuando**: con `n_bars`/`K`/`R` conocidos, los cortes IS/OOS coinciden con la derivación a mano (in-sample creciente, OOS contiguo, sin solaparse); error si `K<1` o `R∉(0,1)` o faltan barras.

## Fase O3: Evaluación en memoria (RF-7)

- [ ] **O3.1**: `evaluate(params, window, settings) -> TradingMetrics` (señales núcleo → `VectorbtEngine.run` → `compute_trading_metrics`), **sin persistir** — depende de `O2.1`
  - **Hecho cuando**: devuelve métricas de una ventana sin escribir en `FT_RESULTS_DIR` (sha256 del dir antes/después, o `tree_digest`, sin cambios); reusa el pipeline mínimo sin alterar el `run` persistido; coincide con el backtest persistido para la misma ventana/params (mismas métricas base).

## Fase O4: Estudio Optuna (RF-3, RF-5, RF-6, RF-8)

- [ ] **O4.1**: Muestreo de parámetros desde el sweep (`suggest_int/float/categorical`, `log`) — depende de `O1.1`
  - **Hecho cuando**: cada tipo de eje produce `suggest_*` correcto; `log=true` usa escala logarítmica; test con un `trial` espía.
- [ ] **O4.2**: `run_study(...)` con `TPESampler(seed)`, dirección de `OBJECTIVES`, score = agregado OOS, constraint `min_trades` y degenerado — depende de `O3.1`, `O4.1`, `O1.1`
  - **Hecho cuando**: objetivo desconocido → `unknown_objective`; `n_trades<min_trades` o métrica no finita → valor degenerado y el estudio **no** aborta (`n_pruned` lo cuenta); un error tipado del motor en un trial → `TrialPruned`; devuelve `OptimizeResult` con `best_params`/`best_score`/`folds`.
- [ ] **O4.3**: Determinismo (RF-8) — depende de `O4.2`
  - **Hecho cuando**: dos `run_study` con la misma semilla, dataset y sweep devuelven el mismo `best_params` y `best_score`.

## Fase O5: CLI (RF-1, RF-9, RF-10)

- [ ] **O5.1**: Comando `ft_vectorbt optimize` con todas las opciones de RF-1 — depende de `O4.2`
  - **Hecho cuando**: `CliRunner`: optimización completa de `sma_cross` sobre el *fixture* devuelve `best_params`/`best_score` (tabla y `--format json`); `--objective` inexistente → exit 2; `--sweep` inválido → exit 2; dataset/estrategia inexistente → exit 1; `--save-best` persiste solo el mejor; progreso a *stderr*, resultado a *stdout*. Opciones introspectables (test por introspección del comando, no del `--help` renderizado).

## Fase O6: Cierre (Art. 20)

- [ ] **O6.1**: Checks del paquete verdes + `test_isolation.py` verde — depende de `O5.1`
  - **Hecho cuando**: `ruff`+`ruff format`+`mypy --strict src`+`pytest -q` verdes; `test_isolation.py` confirma que `optimize.py`/`cli.py` no importan `catalog`/`strategies` ni hermanas.
- [ ] **O6.2**: Matriz Art. 20 en el PR — depende de `O6.1`
  - **Hecho cuando**: 10 dimensiones evidenciadas (Funcional por tests; Arquitectural por aislamiento; Datos float32; Concurrencia = N/A o `n_jobs` futuro; Desempeño = evaluación en memoria; Despliegue = CLI); firmado por el Tech Lead.
