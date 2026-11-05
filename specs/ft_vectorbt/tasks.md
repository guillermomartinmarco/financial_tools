# Tasks — `ft_vectorbt` (MVP v1.0)

> **Convención**: cada tarea es atómica, testeable y sigue TDD (Art. 7). **Hecho cuando**: criterio técnico verificable.
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q` (ver CI).
> Tests en `packages/finances-backtester/tests/ft_vectorbt/`; sin red y con `FT_PROJECT_ROOT` temporal.
> **Prerrequisitos**: tareas `B0.1`–`B7.1` de `specs/finances_backtester/tasks.md` (núcleo compartido en `finances_core.backtest`, incluidas las estrategias y el cálculo de señales) y RFC-0004 aprobada antes de fusionar `engine.py`.
> Estado: 4 de 25 tareas completadas (`V0.1`, `V0.2`, `V0.3`, `V0.4`); el resto pendiente. Esta ronda eliminó las señales por tipo del motor (antes `V1.3`–`V1.5`); las tareas `V1.3`–`V1.5` actuales son nuevas o renumeradas. Las dependencias se indican con «depende de».

## Fase 0: Fundación

- [x] **V0.1**: Verificar prerrequisitos — depende de `B5.1`, `B6.2`
  - **Hecho cuando**: `from finances_core.backtest import catalog, runner, engine_api, results_store, metrics, identity` y `from finances_core.backtest.strategies import BaseStrategy, Signals` importan en `main`; la suite `engine_contract` existe; RFC-0004 figura `Aprobado` o la rama se marca bloqueada para fusión.
  - **Evidencia**: 2026-11-05, sobre `main` a9437f3: los imports del núcleo (`catalog`, `runner`, `engine_api`, `results_store`, `metrics`, `identity`, `BaseStrategy`, `Signals`) funcionan; la suite `tests/contract/engine_contract.py` existe (B6.2); RFC-0004 figura **Aprobado** (2026-11-02). Las tres comprobaciones quedan como regresión en `test_foundation.py`.
- [x] **V0.2**: Resolver y fijar la dependencia `vectorbt` (y NumPy/pandas/Numba transitivos) — depende de `B0.3`
  - **Hecho cuando**: en entornos limpios Python 3.12 de Windows y Ubuntu, `pip install -e "packages/finances-backtester"` instala una versión de `vectorbt` que importa y ejecuta un `Portfolio.from_signals` mínimo; versión elegida, versiones resueltas de NumPy/pandas/Numba, licencia y la cota de `requires-python` quedan registradas **con la salida de `pip list`** en este archivo; el pin se escribe en `pyproject.toml` (`>=X,<Y`) y un test de metadatos (`test_vectorbt_pin.py`) lo fija. **No** se acepta ningún pin sin evidencia.
  - **Evidencia** (03/11/2026):
    - **Prueba limpia**: workflow `vectorbt-probe` (run [41570125514](https://github.com/guillermomartinmarco/financial_tools/actions/runs/41570125514), `pip install vectorbt` sin pin) verde en `windows-latest` y `ubuntu-24.04` con Python 3.12 y 3.13; `.venv` descartable local con Python 3.14.0 (Windows) también. En todas, `Portfolio.from_signals` (30 velas, 3 operaciones, `fees=0.001`) da el mismo resultado: `vectorbt 1.2.0 trades 3 final 104.45895933673323`.
    - **Versiones resueltas** (`pip list`, idénticas en las cuatro celdas del CI y en local):
      ```text
      vectorbt   1.2.0
      numpy      2.5.4
      pandas     3.1.0
      numba      0.68.1
      llvmlite   0.50.0
      scipy      1.18.1
      plotly     7.1.0
      ```
    - **Licencia**: PyPI no la declara; `vectorbt-1.2.0.dist-info/licenses/LICENSE.md` es **Apache 2.0 con *Commons Clause* v1.0** (prohíbe vender el software o un servicio cuyo valor dependa sustancialmente de él). El Tech Lead decidió seguir con uso no comercial; anotado en el README.
    - **Pin y cota**: `vectorbt>=1.2.0,<1.3`, `numpy>=2.5,<3`, `pandas>=3.1,<4` (cotas superiores en la próxima versión mayor para que un NumPy 3 o pandas 4 no rompa `vectorbt` 1.2 sin aviso); `requires-python = ">=3.11,<3.15"` (la cota que declara `vectorbt` 1.2.0, que cierra A16). `tests/ft_vectorbt/test_vectorbt_pin.py` fija el pin (RED antes de declararlo, GREEN después).
    - **Matriz completa**: `vectorbt-probe` con el pin `vectorbt>=1.2.0,<1.3` (run [41571990385](https://github.com/guillermomartinmarco/financial_tools/actions/runs/41571990385)) verde en `windows-latest` y `ubuntu-24.04` con Python 3.12, 3.13 y 3.14, mismo resultado en las seis celdas.
    - **Instalación del paquete**: CI de la rama `feat/vectorbt-pin` (run [41571938036](https://github.com/guillermomartinmarco/financial_tools/actions/runs/41571938036)) verde en Windows y Ubuntu con Python 3.12; `pip install -e "packages/finances-backtester[dev]"` instala `vectorbt-1.2.0` y `test_vectorbt_pin.py` pasa en ambos.
- [x] **V0.3**: Esqueleto `ft_vectorbt/` (`__init__` con `ENGINE_NAME`, `cli` con flags globales, `api.router` con prefijo `/backtest/vectorbt`, `engine.py` vacío) y script `ft_vectorbt` — depende de `V0.2`
  - **Hecho cuando**: `ft_vectorbt --help` responde y `test_foundation.py` importa `cli` y `api.router`.
  - **Evidencia**: `ft_vectorbt/` con `ENGINE_NAME = "vectorbt"`, `cli.py` (Typer; flags globales `-v`/`-q`, `--log-format rich|json`, `--format table|json` validados al parsear), `api.router` con prefijo `/backtest/vectorbt`, `engine.py` vacío y script `ft_vectorbt` en `pyproject.toml`; `ft_vectorbt --help` responde (exit 0). `test_foundation.py` (20).
- [x] **V0.4**: Aislamiento Art. 4 y RFC-0004 (test AST) — depende de `V0.3`
  - **Hecho cuando**: `test_isolation.py` falla si algún módulo de `ft_vectorbt` importa `ft_*`, `finances_backtester.ft_*` hermanas, `finances_downloader`, `finances_analytics`, `finances_api`, `finances_core.backtest.strategies` o `finances_core.backtest.catalog`, o si `pandas`/`numpy`/`vectorbt` o nombres de doble precisión aparecen fuera de `engine.py`; pasa sobre el esqueleto.
  - **Evidencia**: `test_isolation.py` (34): rechaza por AST (imports absolutos, relativos y `importlib`) `ft_*`, hermanas `finances_backtester.ft_*`, `finances_downloader`, `finances_analytics`, `finances_api`, `strategies`/`catalog` (reutiliza `engine_contract.engine_import_violations`) y `pandas`/`numpy`/`vectorbt` o nombres de doble precisión (`float64`, `Float64`, `double`; mismo conjunto que RFC-0002) fuera de `engine.py`; el `__init__` del paquete cuenta como fuera del borde; pasa sobre el esqueleto.

## Fase 1: Motor — solo simulación (RF-1, RF-2, RF-3) — requiere RFC-0004 aprobada

- [ ] **V1.1**: Puntos de conversión `to_framework_input(candles, signals)` y `from_framework_output` — depende de `V0.4`, `B1.2`, `B3.1`
  - **Hecho cuando**: tests: ida y vuelta `Float32` → doble precisión → `Float32` idéntica bit a bit; `entries`/`exits` `Boolean` → arreglos booleanos → idénticas; `open_time_ms` nunca pasa por el framework (índice posicional); salida con `NaN`/`inf`, `side` fuera de dominio o `exit_reason` fuera de dominio → `output_out_of_range`; `test_engine_boundary.py` (AST) confirma que la ampliación solo está en `to_framework_input` y el *downcast* solo en `from_framework_output`.
- [ ] **V1.2**: `simulate` con VectorBT: desplazamiento `t→t+1`, ejecución a apertura, comisiones, deslizamiento, tamaño `size_fraction`, descarte de entradas en la última barra y liquidación final al cierre (RF-2) — depende de `V1.1`
  - **Hecho cuando**: tests con series de 5–10 barras calculadas a mano (con y sin costes, posición abierta al final, señal en la última barra, `entry` con posición abierta ignorada); se **verifica y documenta en el plan §3.3** cómo VectorBT descuenta comisiones al dimensionar y, si difiere de RF-5.5, el ajuste aplicado; `trades` y `equity` canónicos cumplen `validate_engine_output`.
- [ ] **V1.3**: Agnosticismo del motor: ejecutar señales arbitrarias sin conocer la estrategia (RF-1) — depende de `V1.2`, `B5.1`
  - **Hecho cuando**: `test_engine_signals_agnostic.py` ejecuta `simulate` y `engine.run` con las señales de la estrategia sintética de la suite de contrato y con señales aleatorias sembradas y obtiene `trades`/`equity` válidos; la firma de `VectorbtEngine.run` no tiene `kind`/`params`/estrategia; `grep` confirma que `engine.py` no define `KINDS_IMPL` ni `supported_kinds` ni calcula medias, RSI o canales.
- [ ] **V1.4**: `VectorbtEngine` (`name`, `version()`, `run(candles, signals, …)`) — depende de `V1.2`, `V1.3`
  - **Hecho cuando**: `engine.run` sobre el *fixture*, con las señales del núcleo de las 3 estrategias integradas, devuelve `EngineOutput` válido; `version()` coincide con el pin de V0.2.
- [ ] **V1.5**: Test de precisión (RFC-0004) — depende de `V1.4`, `B6.1`
  - **Hecho cuando**: resultados del motor frente al *golden* dentro de las tolerancias de RFC-0004 punto 5 en las 3 estrategias integradas; las tolerancias calibradas con evidencia (diferencias máximas observadas) quedan registradas en este archivo.

## Fase 2: Servicio y despacho (RF-4, RF-5, RF-8)

- [ ] **V2.1**: `service.run_sync` sobre `run_backtest` — depende de `V1.4`, `B5.1`
  - **Hecho cuando**: tests con almacén y `strategies.db` temporales: resultado completo en disco; repetición → `cached = true` sin invocar a VectorBT (espía); `force` recalcula con `trades.parquet`/`equity.parquet`/`summary.json` idénticos byte a byte; mismo `run_id` concurrente deja un único resultado; no se escribe bajo `FT_PARQUET_DIR`.
- [ ] **V2.2**: `service.dispatch` con `SQLTaskManager` (fases `LOADING → SIGNALS → SIMULATING → PERSISTING`) y semáforo `FT_BACKTEST_MAX_CONCURRENT` (Art. 3, 12, 16) — depende de `V2.1`, `B5.2`
  - **Hecho cuando**: tests: tarea `COMPLETED` con resumen (`run_id`, `cached`, `n_bars`, `n_trades`); fase nunca retrocede; excepción simulada de VectorBT → `FAILED`; `output_out_of_range` → `FAILED`; excepción en `signals` de una estrategia local de prueba → `FAILED`; con límite 1, la segunda corrida espera; se comprueba si `TaskRecorder` es genérico para estas fases y, si no, se extrae a `finances_core.backtest.dispatch` sin cambiar los tests de los consumidores actuales.
- [ ] **V2.3**: Logging y auditoría (RF-10) — depende de `V2.1`
  - **Hecho cuando**: test captura el evento `backtest_run` con todos los campos de `finances_backtester` RF-13 (incluidos `kind_version`, `strategy_code_hash`, `strategy_origin`, `signals_ms`) y `engine="vectorbt"` en los resultados `completed`, `cached` y `failed`; el contexto incluye `task_id`, `dataset`, `run_id`.

## Fase 3: CLI (RF-6, RF-9)

- [ ] **V3.1**: `run` y `kinds [--kind] [--check]` — depende de `V2.1`, `B1.6`
  - **Hecho cuando**: `ft_vectorbt run --help` lista las opciones de RF-6; tests con `CliRunner`: ejecución completa, `--request-file`, `--force`, `--format json`; exit 1 (estrategia/dataset inexistente), 2 (`unknown_kind`, `invalid_params`, `strategy_init_error`, costes negativos, rango inválido/vacío), 3 (excepción simulada, `strategy_runtime_error`, `invalid_signals`), 4 (IO); `kinds` lista integradas y locales en orden determinista e incluye las carpetas inválidas con su error; `kinds --check` sobre un `FT_STRATEGIES_DIR` temporal sale 0 con una estrategia válida y 2 con una con anticipación (`shift(-1)`) o con README incompleto; ninguna opción acepta una ruta de código.
- [ ] **V3.2**: `runs`, `show`, `trades`, `equity`, `status`, `result` — depende de `V3.1`, `B4.2`
  - **Hecho cuando**: tests: listado con filtros y cursor; `show` de `run_id` inexistente → exit 1; `trades`/`equity` paginan con `--after-ms`; `status`/`result` sobre tareas de la API.

## Fase 4: API (RF-7, RF-8, RF-9)

- [ ] **V4.1**: `POST /run`, `GET /status/{id}`, `GET /result/{id}` — depende de `V2.2`
  - **Hecho cuando**: tests con `httpx.AsyncClient`: `202` con `task_id`, `status="ACCEPTED"`, `poll_url`, `result_url` sin calcular en el endpoint (espía de despacho; techo de CI `DISPATCH_CEILING_MS = 2000`, ver la deriva de `ft_indicators` T5.4; el ≤ 200 ms del Art. 18 es objetivo de producción) (cálculo simulado lento); *polling* hasta `COMPLETED`; `/result` no terminado → 409; inexistente → 404; tarea `FAILED` → 500; estrategia inexistente → 404 sin crear tarea; `unknown_kind`/`invalid_params` → 400/422; `strategy_init_error` → 500; `BacktestRequest` con campos de código/ruta (`code`, `path`, `module`) → 422.
- [ ] **V4.2**: `GET /kinds` (solo lectura, sin ningún endpoint de carga de código), `GET /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/trades`, `GET /runs/{run_id}/equity` — depende de `V4.1`, `B4.2`
  - **Hecho cuando**: tests: filtros, paginación (`limit` 1..10000, `after_ms`, `next_after_ms`), 404, 422 con `limit` fuera de rango; el event loop no se bloquea en lecturas grandes (`run_in_threadpool`).
- [ ] **V4.3**: Contrato CLI↔API — depende de `V3.2`, `V4.2`
  - **Hecho cuando**: `test_contract.py` compara resumen, `trades` y `equity` de una misma petición vía CLI (`--format json`) y API, sin duplicar lógica (Art. 1).

## Fase 5: Integración con la suite

- [ ] **V5.1**: Montar el router en `finances-api` (`/backtest/vectorbt`), añadir `finances-backtester` a sus dependencias y actualizar `specs/finances_api` (ver `P5`) — depende de `V4.2`
  - **Hecho cuando**: `pytest packages/finances-api -v` pasa; `/openapi.json` incluye las 8 rutas de plan §2.2; `poll_url`/`result_url` del `202` apuntan a `/backtest/vectorbt/...`; `test_composed_app_route_names_are_unique` sigue verde (rutas `vectorbt_*`).
- [ ] **V5.2**: CI (`.github/workflows/ci.yml`): instalación editable de `finances-backtester[dev]` antes de `finances-api`, entrada en el bucle mypy/pytest, paso `Smoke vectorbt import` — depende de `V0.2`
  - **Hecho cuando**: CI verde en `windows-latest` y `ubuntu-24.04` con los tests de `ft_vectorbt` ejecutándose; el log muestra la versión de VectorBT importada.

## Fase 6: Validación cruzada y determinismo (RF-5, RF-13)

- [ ] **V6.1**: Suite de contrato sobre el *fixture* para `sma_cross`, `rsi_reversion`, `donchian_breakout` y la estrategia sintética — depende de `V1.5`, `B6.2`
  - **Hecho cuando**: `engine_contract.assert_engine_matches_golden(VectorbtEngine(), kind)` pasa en Windows y Ubuntu; cada estrategia integrada con ≥ 5 operaciones; tolerancias de RFC-0004; la estrategia sintética pasa sin código específico en `ft_vectorbt`.
- [ ] **V6.2**: Determinismo e idempotencia end-to-end — depende de `V2.1`
  - **Hecho cuando**: dos ejecuciones con `--force` producen archivos con el mismo SHA-256; si Numba introduce no determinismo, se fija la configuración y se documenta en el plan §3 (RF-5).
- [ ] **V6.3**: Smoke con datos reales (BTCUSDT `1h` descargado con `ft_market_data_binance`, estrategia creada con `ft_strategy_manager`): `run`, repetir (`cached`), `show`, `trades`, `ft_serve` expone `/backtest/vectorbt` — depende de `V5.1`
  - **Hecho cuando**: salida registrada en este archivo (rango, `n_trades`, métricas, `run_id`, tiempos); la repetición no invoca a VectorBT; los archivos coinciden con una réplica `--force`.

## Fase 7: Desempeño, documentación y CI

- [ ] **V7.1**: Benchmark RF-12 (1 000 000 de velas `1m`, señales de `sma_cross(20, 100)` precalculadas por el núcleo) con umbral calibrado — depende de `V1.4`
  - **Hecho cuando**: `thresholds.json` con 1,5 × mediana de 5 repeticiones en `ubuntu-24.04`; paso de CI con `BENCH_ENFORCE=1` falla ante regresión; resultado subido como artefacto.
- [ ] **V7.2**: Documentación: sección de `ft_vectorbt` en README (`P4`), ejemplos de `run`/API, nota de RFC-0004, tabla de estrategias integradas y enlace a `docs/guides/strategy-authoring.md` — depende de `V6.3`
  - **Hecho cuando**: los ejemplos del README se ejecutan tal cual contra un almacén temporal; `ft_vectorbt --help` y `/docs` describen todas las opciones y rutas.

## Fase 8: Cierre (Art. 20)

- [ ] **V8.1**: Matriz Art. 20 en el checklist del PR — depende de todas las anteriores
  - **Hecho cuando**: las 10 dimensiones marcadas con evidencia: Funcional (RF-1…RF-13 con tests), Contractual (specs firmadas, A1–A27 resueltos), Arquitectural (Art. 1 por `V4.3`, Art. 4 por `V0.4`), Datos (RFC-0004 aprobada, dtypes por `V1.1`, Pydantic), Concurrencia (`V2.1`–`V2.2`), Observabilidad (`V2.3`; métricas VRAM = N/A justificado, sin GPU), Resiliencia = N/A justificado (sin red), Calidad (ruff, `mypy --strict`, cobertura ≥ 90 % de la lógica de servicio), Desempeño (`V7.1`), Despliegue (`V4.1`, `V5.1`); checklist firmado por el Tech Lead.
