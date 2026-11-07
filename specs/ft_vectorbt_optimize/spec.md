# Spec — `ft_vectorbt optimize` (optimizador de parámetros) — Slice 2

## Contexto e Historias de Usuario

Subcomando **`ft_vectorbt optimize`** que **optimiza los parámetros** de **una** estrategia
técnica registrada sobre **un** dataset (mono-activo), con **Optuna** (sampler TPE sembrado),
**walk-forward anclado** (validación fuera de muestra, anti-overfitting) y **objetivo elegible**
del catálogo de métricas (Slice 1, `backtest_metrics`). Reutiliza el backtest de `ft_vectorbt`
como **función de fitness**; no introduce motor nuevo ni capacidades cross-seccionales.

Vive **dentro** de `ft_vectorbt` (respeta el aislamiento: no importa herramientas hermanas; usa
`finances_core.backtest.{runner, metrics, trading_metrics, strategy_source}` y el motor propio).
`optuna` pasa a ser dependencia de `finances-backtester`.

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant | Como quant, **quiero** `ft_vectorbt optimize` que busque los mejores parámetros de una estrategia sobre un dataset **para** no ajustarlos a mano. |
| US-2 | Quant | Como quant, **exijo** validación **fuera de muestra** (walk-forward anclado) y un **mínimo de operaciones** **para** no sobreajustar. |
| US-3 | Quant | Como quant, **quiero** elegir la métrica objetivo (Calmar, Sharpe, Profit Factor, …) **para** optimizar según mi criterio de riesgo. |
| US-4 | Operador | Como operador, **necesito** que sea **determinista por semilla** y que los trials inválidos no rompan el estudio **para** reproducir y confiar en el resultado. |

---

## Alcance (cerrado con el usuario)

- **Mono-activo, técnico, OHLCV.** Nada cross-seccional, nada genético, nada de los 101 alphas.
- **CLI primero**; la API (`POST /backtest/vectorbt/optimize`) queda para un slice posterior.
- **Walk-forward anclado obligatorio** (no opcional): un optimizador sin OOS es una fábrica de
  sobreajuste.
- Objetivo **único** por corrida (multi-objetivo / Pareto fuera de alcance).
- Depende de **Slice 1** (`backtest_metrics`: `OBJECTIVES`, `objective_value`, `TradingMetrics`).

---

## Requisitos Funcionales (EARS)

### RF-1 — Comando `optimize` (Ubicuo, Art. 1)
**EL SISTEMA DEBE** proveer `ft_vectorbt optimize` con:
`-d/--dataset ex/mt/sym/tf`, `--strategy-id ID` (o `--kind KIND` para una integrada con sus
defaults como base), `--sweep FILE.json` (espacio de búsqueda), `--objective NAME`
(por defecto **`calmar`**), `--n-trials N`, `--folds K`, `--in-sample-ratio R`,
`--min-trades M`, `--fold-agg {mean|min|median}` (por defecto `mean`), `--seed S`,
`--start-ms`/`--end-ms`, `--fees-bps`/`--slippage-bps`/`--initial-cash`/`--size-fraction`,
`--format table|json`. Salida: mejores parámetros, score, desglose por fold y métricas
in-sample vs OOS.

### RF-2 — Espacio de búsqueda declarativo (Ubicuo)
**EL SISTEMA DEBE** aceptar un `sweep` JSON con **ejes** = parámetros de la estrategia, cada uno
`int`/`float` con `{low, high, step?, log?}` o `categorical` con `{choices[]}`. Los parámetros
**DEBEN** existir en el esquema `Params` de la estrategia; **SI** un eje no existe o su rango es
inválido **ENTONCES** `invalid_sweep` (exit 2) **antes** de correr cualquier trial.

### RF-3 — Optuna TPE determinista (Estado-Driven)
**EL SISTEMA DEBE** usar `optuna` con `TPESampler(seed=S)` (interfaz ask/tell), `n_trials` por
fold; **DEBE** ser reproducible: misma semilla + mismo dataset + mismo sweep → mismo mejor
resultado.

### RF-4 — Walk-forward anclado (Ubicuo, anti-overfitting)
**EL SISTEMA DEBE** derivar `K` folds anclados sobre el rango efectivo: in-sample **creciente**
desde el inicio + out-of-sample a continuación (`in_sample_ratio` R). La **búsqueda** evalúa el
objetivo sobre **OOS** y el **score del combo** es el agregado (`--fold-agg`) de los folds OOS.
**EL SISTEMA DEBE** reportar, para el mejor set, la métrica in-sample y OOS por fold (para ver
la brecha de sobreajuste).

### RF-5 — Objetivo del catálogo (Ubicuo)
**EL SISTEMA DEBE** resolver `--objective` contra `backtest_metrics.OBJECTIVES` (Slice 1),
respetando su **dirección** (max/min). **SI** el nombre no está en el registro **ENTONCES**
`unknown_objective` (exit 2). Los diagnósticos (`z_score`, `trade_count`) **NO** son objetivos.

### RF-6 — Trials inválidos y constraint de operaciones (Evento-Driven)
**SI** un trial produce un resultado inválido (error tipado de la estrategia/motor, métrica no
finita) **o** `n_trades < --min-trades` **ENTONCES** su valor-objetivo es el **degenerado**
(`objective_value`) y el estudio **continúa** (no aborta). Un error de datos/dataset inexistente
sí detiene el comando (exit 1).

### RF-7 — Evaluación en memoria (Ubicuo, RF-5 de `ft_vectorbt`)
**EL SISTEMA DEBE** evaluar cada trial **en memoria** (señales del núcleo → motor → métricas de
Slice 1 sobre OOS) **sin persistir** cada corrida en `FT_RESULTS_DIR`. **EL SISTEMA NO DEBE**
escribir miles de resultados; a lo sumo persiste/informa **el mejor** (opcional, `--save-best`).

### RF-8 — Determinismo e idempotencia (Art. 12)
Dos corridas con la misma semilla, dataset y sweep **DEBEN** dar el mismo mejor set y score.

### RF-9 — Errores y códigos de salida
Tabla de `ft_vectorbt` RF-9 + particulares: `invalid_sweep`/`unknown_objective`/rango vacío →
exit 2; dataset/estrategia inexistente → exit 1; error inesperado → exit 3; IO → exit 4.

### RF-10 — Logging (Art. 9)
Progreso (trial, mejor score) por `structlog` a *stderr*; resultado a *stdout* (tabla/JSON).

---

## Casos límite / No-Go

| Tema | Decisión |
|------|----------|
| Multi-objetivo / Pareto (NSGA-II) | Fuera de alcance |
| Cross-seccional / multi-activo / genético / 101 alphas | Fuera de alcance |
| API HTTP del `optimize` | Slice posterior (CLI primero) |
| Persistir cada trial | Prohibido (RF-7); solo el mejor opcional |
| Treynor / Beta / benchmark | Fuera de alcance (Slice 1) |
| GPU/CUDA | No se usa (Optuna CPU; corre en Jetson arm64) |
