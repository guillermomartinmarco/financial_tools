# Plan — `ft_vectorbt optimize` (optimizador de parámetros) — Slice 2

## 1. Estructura

```
packages/finances-backtester/src/finances_backtester/ft_vectorbt/
  cli.py        # EXISTE: se AÑADE el comando `optimize`
  service.py    # EXISTE: se AÑADEN helpers de optimización (evaluación en memoria + estudio)
  optimize.py   # NUEVO: motor de optimización (sweep, walk-forward, estudio Optuna)
packages/finances-backtester/tests/ft_vectorbt/
  test_optimize.py     # NUEVO: sweep, folds, degenerados, determinismo, CLI
pyproject.toml  # + dependencia optuna
specs/ft_vectorbt_optimize/  # este tridente
```

Aislamiento: `optimize.py` vive en `ft_vectorbt`; importa `optuna`, `polars`,
`finances_core.backtest.{runner, strategy_source, trading_metrics}` y el motor propio
`VectorbtEngine`. **No** importa `catalog`/`strategies` directamente (usa `strategy_source`,
RF-21) ni herramientas hermanas (Art. 4). `test_isolation.py` debe seguir verde.

## 2. CLI (`ft_vectorbt optimize`)

| Opción | Tipo | Default | Nota |
|---|---|---|---|
| `-d/--dataset` | `ex/mt/sym/tf` | — | requerido |
| `--strategy-id` / `--kind` | str | — | uno de los dos (kind usa defaults como base) |
| `--sweep` | path JSON | — | espacio de búsqueda (RF-2) |
| `--objective` | str | `calmar` | nombre en `OBJECTIVES` |
| `--n-trials` | int | 50 | por fold |
| `--folds` | int | 4 | walk-forward anclado |
| `--in-sample-ratio` | float (0,1) | 0.75 | |
| `--fold-agg` | `mean\|min\|median` | `mean` | agregado OOS |
| `--min-trades` | int | 10 | constraint (RF-6) |
| `--seed` | int | 42 | determinismo |
| `--save-best` | flag | off | persiste el mejor run (RF-7) |
| costos/caja/rango/`--format` | — | — | como `run` |

Salida (tabla/JSON): `best_params`, `best_score`, `objective`, `direction`, por fold
`{in_sample, oos}` del objetivo, `n_trials`, `n_pruned`, `seed`.

## 3. Contratos (Pydantic / dataclasses)

```python
class SweepAxis(BaseModel):
    param: str
    type: Literal["int", "float", "categorical"]
    low: float | None = None
    high: float | None = None
    step: float | None = None
    log: bool = False
    choices: list[float | int | str] | None = None

class SweepSpec(BaseModel):
    axes: list[SweepAxis]           # no vacío; `param` únicos; deben existir en Params de la estrategia

class FoldScore(BaseModel):
    fold: int
    in_sample: float | None
    oos: float | None

class OptimizeResult(BaseModel):
    objective: str
    direction: Direction
    best_params: dict[str, ParamScalar]
    best_score: float
    folds: list[FoldScore]
    n_trials: int
    n_pruned: int
    seed: int
```

## 4. Lógica principal

### 4.1 Validación del sweep (RF-2)
`validate_sweep(spec, kind, registry_via_strategy_source)`: cada `axis.param` existe en el
esquema `Params` de la estrategia; rangos válidos (`low<high`, `step>0`, choices no vacío);
tipos coherentes. Error → `InvalidSweepError` (exit 2) **antes** de crear el estudio.

### 4.2 Folds anclados (RF-4)
`anchored_folds(n_bars, n_folds, in_sample_ratio) -> list[(is_slice, oos_slice)]`:
in-sample creciente desde el inicio; OOS inmediatamente después. Derivación documentada
(`r = ratio/(1−ratio)`, como el proyecto de referencia). Se traducen a rangos `start_ms/end_ms`
sobre el `open_time_ms` del dataset.

### 4.3 Evaluación en memoria (RF-7)
`evaluate(params, window) -> TradingMetrics`:
señales del núcleo (`strategy_source` + estrategia) sobre las velas de `window` → `VectorbtEngine.run`
→ `trading_metrics.compute_trading_metrics(...)`. **No** usa `run_backtest`/persistencia; corre el
pipeline mínimo en memoria. (Si hace falta reutilizar lógica del runner sin persistir, se extrae un
helper puro; no se cambia el comportamiento del `run` persistido.)

### 4.4 Estudio Optuna (RF-3, RF-5, RF-6)
```
study = optuna.create_study(direction=OBJECTIVES[name].direction, sampler=TPESampler(seed=S))
def trial_fn(trial):
    params = sample(trial, sweep)                 # suggest_int/float/categorical por eje
    oos_scores = []
    for is_slice, oos_slice in folds:
        m = evaluate(params, oos_slice)           # (el IS se usa para el reporte; la búsqueda puntúa OOS)
        if m.trade_count < min_trades: return DEGENERATE
        v = objective_value(name, m)
        oos_scores.append(v)
    return aggregate(oos_scores, fold_agg)        # NaN/degenerado si algún fold inválido
study.optimize(trial_fn, n_trials=n_trials)       # excepciones tipadas -> TrialPruned/degenerado
```
`DEGENERATE` según dirección (via `objective_value`). Trials que lanzan errores tipados del
dominio → `optuna.TrialPruned` (no abortan). El reporte recalcula in-sample del mejor set.

### 4.5 Salida
`_print_optimize(result, fmt)`: tabla Rich (mejor set + folds IS/OOS) o `model_dump_json`.

## 5. Dependencias / CI
- `optuna` en `packages/finances-backtester/pyproject.toml` (dependencias). Verificar que
  instala en windows-latest y ubuntu-24.04 (y arm64/Jetson, Optuna es Python puro).
- Smoke opcional de import de optuna en CI (análogo a vectorbt).

## 6. ADR
| ID | Decisión | Razón |
|----|----------|-------|
| ADR-O1 | Subcomando de `ft_vectorbt`, no tool nuevo | Aislamiento: reusa motor/servicio sin importar hermanos |
| ADR-O2 | Optuna TPE sembrado (no reimplementar) | Python puro, simple, ya en uso por el usuario (Jetson) |
| ADR-O3 | Walk-forward anclado obligatorio; score OOS | Anti-overfitting (lección #1 del proyecto de referencia) |
| ADR-O4 | Evaluación en memoria sin persistir trials | RF-7: miles de trials sin ensuciar `FT_RESULTS_DIR` |
| ADR-O5 | Objetivo del catálogo Slice 1 (dirección+degenerado) | Una sola definición de métricas; trials inválidos nunca ganan |
| ADR-O6 | CLI primero; API después | Paso chico; la dualidad Art. 1 se completa en slice posterior |

## 7. Riesgos
- **Sobreajuste**: mitigado por walk-forward + `--min-trades` + reporte IS vs OOS. Documentar que
  un buen OOS no garantiza futuro.
- **Costo de cómputo**: `n_trials × folds × backtest`; evaluación en memoria lo acota; paralelismo
  (Optuna `n_jobs`) queda para un slice posterior.
- **Determinismo**: `TPESampler(seed)` + backtest byte-idéntico; verificar en test.
- **Extraer helper de evaluación sin persistir**: no debe alterar el `run` persistido existente.
