# Tasks — `ft_vectorbt optimize` (optimizador de parámetros) — Slice 2

> **Convención**: cada tarea es atómica, testeable y sigue TDD (Art. 7). **Hecho cuando**: criterio verificable.
> Checks: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` en `packages/finances-backtester`; `test_isolation.py` DEBE seguir verde.
> Tests en `packages/finances-backtester/tests/ft_vectorbt/test_optimize.py` (reusa las *fixtures* de `conftest`: `FT_PROJECT_ROOT` temporal, *fixture* Hive, `strategies.db` temporal). Sin red.
> **Prerrequisitos**: **Slice 1 (`metrics`, módulo `finances_core.metrics`) mergeado** (`OBJECTIVES`, `objective_value`, `TradingMetrics`); `ft_vectorbt` fases 0–7 (en `main`).

## Fase O0: Dependencia y contratos

- [x] **O0.1**: Añadir `optuna` a `packages/finances-backtester/pyproject.toml` y fijar el pin con evidencia (`pip list`) — depende de Slice 1
  - **Hecho cuando**: `pip install -e "packages/finances-backtester[dev]"` instala `optuna`; `python -c "import optuna; print(optuna.__version__)"` responde; versión y cota registradas aquí; (opcional) smoke de import en `ci.yml` análogo a vectorbt.
- [x] **O0.2**: Modelos `SweepAxis`, `SweepSpec`, `FoldScore`, `OptimizeResult` (plan §3) en `optimize.py` — depende de `O0.1`
  - **Hecho cuando**: importan y validan con Pydantic (`extra="forbid"`); `SweepSpec` rechaza ejes vacíos y `param` duplicados; `mypy --strict` verde.

## Fase O1: Espacio de búsqueda (RF-2)

- [x] **O1.1**: `validate_sweep(spec, kind, settings)` contra el esquema `Params` de la estrategia (vía `strategy_source`) — depende de `O0.2`
  - **Hecho cuando**: un eje inexistente, un rango inválido (`low>=high`, `step<=0`) o `choices` vacío lanzan `InvalidSweepError`; un sweep válido de `sma_cross` (`fast`,`slow`) pasa; **no** importa `catalog`/`strategies` (aislamiento).

## Fase O2: Walk-forward anclado (RF-4)

- [x] **O2.1**: `anchored_folds(n_bars, n_folds, in_sample_ratio)` + traducción a rangos `start_ms/end_ms` — depende de `O0.2`
  - **Hecho cuando**: con `n_bars`/`K`/`R` conocidos, los cortes IS/OOS coinciden con la derivación a mano (in-sample creciente, OOS contiguo, sin solaparse); error si `K<1` o `R∉(0,1)` o faltan barras.

## Fase O3: Evaluación en memoria (RF-7)

- [x] **O3.1**: `evaluate(params, window, settings) -> TradingMetrics` (señales núcleo → `VectorbtEngine.run` → `compute_trading_metrics`), **sin persistir** — depende de `O2.1`
  - **Hecho cuando**: devuelve métricas de una ventana sin escribir en `FT_RESULTS_DIR` (sha256 del dir antes/después, o `tree_digest`, sin cambios); reusa el pipeline mínimo sin alterar el `run` persistido; coincide con el backtest persistido para la misma ventana/params (mismas métricas base).

## Fase O4: Estudio Optuna (RF-3, RF-5, RF-6, RF-8)

- [x] **O4.1**: Muestreo de parámetros desde el sweep (`suggest_int/float/categorical`, `log`) — depende de `O1.1`
  - **Hecho cuando**: cada tipo de eje produce `suggest_*` correcto; `log=true` usa escala logarítmica; test con un `trial` espía.
- [x] **O4.2**: `run_study(...)` con `TPESampler(seed)`, dirección de `OBJECTIVES`, score = agregado OOS, constraint `min_trades` y degenerado — depende de `O3.1`, `O4.1`, `O1.1`
  - **Hecho cuando**: objetivo desconocido → `unknown_objective`; `n_trades<min_trades` o métrica no finita → valor degenerado y el estudio **no** aborta (`n_pruned` lo cuenta); un error tipado del motor en un trial → `TrialPruned`; devuelve `OptimizeResult` con `best_params`/`best_score`/`folds`.
- [x] **O4.3**: Determinismo (RF-8) — depende de `O4.2`
  - **Hecho cuando**: dos `run_study` con la misma semilla, dataset y sweep devuelven el mismo `best_params` y `best_score`.

## Fase O5: CLI (RF-1, RF-9, RF-10)

- [x] **O5.1**: Comando `ft_vectorbt optimize` con todas las opciones de RF-1 — depende de `O4.2`
  - **Hecho cuando**: `CliRunner`: optimización completa de `sma_cross` sobre el *fixture* devuelve `best_params`/`best_score` (tabla y `--format json`); `--objective` inexistente → exit 2; `--sweep` inválido → exit 2; dataset/estrategia inexistente → exit 1; `--save-best` persiste solo el mejor; progreso a *stderr*, resultado a *stdout*. Opciones introspectables (test por introspección del comando, no del `--help` renderizado).

## Fase O6: Cierre (Art. 20)

- [x] **O6.1**: Checks del paquete verdes + `test_isolation.py` verde — depende de `O5.1`
  - **Hecho cuando**: `ruff`+`ruff format`+`mypy --strict src`+`pytest -q` verdes; `test_isolation.py` confirma que `optimize.py`/`cli.py` no importan `catalog`/`strategies` ni hermanas.
- [x] **O6.2**: Matriz Art. 20 en el PR — depende de `O6.1`
  - **Hecho cuando**: 10 dimensiones evidenciadas (Funcional por tests; Arquitectural por aislamiento; Datos float32; Concurrencia = N/A o `n_jobs` futuro; Desempeño = evaluación en memoria; Despliegue = CLI); firmado por el Tech Lead.

## Evidencia (Slice 2 implementado)

- **Deriva (Art. 15):** la evaluación en memoria vive en `finances_core.backtest.evaluate.evaluate_params(kind, params, candles, engine, costs, …)` (el optimizador varía los `params` de un *kind*, lo que requiere la **clase** de la estrategia del registro, inaccesible desde `ft_vectorbt` por RF-21). `ft_vectorbt.optimize` la consume con el `VectorbtEngine`.
- **Truco "un solo run, ventanas de métricas":** un único `engine.run` por trial sobre el histórico completo (warmup correcto) y métricas por **ventana OOS** recalculando el drawdown relativo a la ventana (`_window_value`). El constraint `--min-trades` aplica a las operaciones del run completo.
- **O0:** `optuna>=4.0,<5` (verificado **optuna 4.9.0**) en `finances-backtester`; helper `evaluate_params` en el núcleo.
- **O1–O2:** `SweepSpec`/`SweepAxis` (Pydantic `extra="forbid"`, validan rangos y duplicados), `validate_sweep` contra el esquema `Params` vía `strategy_source.kinds()`, `anchored_folds` (folds anclados).
- **O3–O4:** `evaluate_params` en memoria (sin persistir); estudio Optuna `TPESampler(seed)` con dirección mapeada (`maximize`/`minimize`), objetivo del catálogo `finances_core.metrics.OBJECTIVES`, trials inválidos → degenerado sin abortar (`n_pruned`), determinismo por semilla.
- **O5:** comando `ft_vectorbt optimize` (todas las opciones de RF-1; objetivo/sweep inválidos → exit 2; estrategia/dataset inexistente → exit 1; exactamente uno de `--strategy-id`/`--kind`).
- **O6:** `ruff` + `ruff format` + `mypy --strict` verdes; `test_optimize.py` (9 casos: sweep, folds, determinismo, constraint, objetivo desconocido, CLI); `test_isolation.py` verde (`optimize.py`/`cli.py` no importan `catalog`/`strategies`). Regresión: finances-core **1280**, finances-backtester **345 passed**.

## Enmienda E1 — `--start`/`--end` amigables (reemplazan `--start-ms`/`--end-ms`)

- [ ] **E1.O** — el comando `optimize` expone `--start`/`--end` (ISO 8601 | ms epoch | `now`) en lugar de `--start-ms`/`--end-ms`, convertidos a ms con `finances_core.timeutils.parse_time_input` antes de `anchored_folds`.
  - **Hecho cuando**: `CliRunner`: `--start 2024-01-01` acota el histórico y `--start basura` → exit 2; `test_optimize.py` verde.
