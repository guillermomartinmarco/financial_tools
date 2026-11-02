# Tasks — `finances-backtester` (contratos compartidos, MVP v1.0)

> **Convención**: cada tarea es atómica, testeable y sigue TDD (Art. 7). **Hecho cuando**: criterio técnico verificable.
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q`.
> Las tareas `B*` (núcleo compartido) **preceden** a las `V*` de `specs/ft_vectorbt/tasks.md`; cada tarea indica su dependencia.
> Tests del núcleo en `packages/finances-core/tests/unit/`; sin red y con `FT_PROJECT_ROOT` temporal.
> Estado: todas pendientes (diseño, sin código).

## Fase 0: Gobernanza y esqueleto

- [x] **B0.1**: Aprobar RFC-0004 y la enmienda del Art. 8 (constitución 1.2.0) — **bloquea todo `engine.py`**, no el núcleo
  - **Hecho cuando**: `docs/rfc/RFC-0004-backtest-engines-float64-boundary.md` figura `Aprobado` con firma del Tech Lead (ver P1).
  - **Hecho**: RFC-0004 aprobado por el Tech Lead el 2026-11-02 (Estado `Aprobado`).
- [ ] **B0.2**: Revisar y resolver los Supuestos A1–A20 de `spec.md`
  - **Hecho cuando**: cada fila de «Supuestos y Decisiones Abiertas» queda marcada `Confirmado` o `Vetado` (con el cambio aplicado a spec/plan) y las specs se firman (Art. 20, Contractual).
- [ ] **B0.3**: Crear `packages/finances-backtester` (pyproject según plan §4, `src/finances_backtester/__init__.py` con `__version__`, `py.typed`, `tests/`) sin dependencias de motor — depende de B0.2
  - **Hecho cuando**: `pip install -e "packages/finances-backtester[dev]"` funciona, `python -c "import finances_backtester"` pasa y `ruff`/`mypy --strict src` son verdes.

## Fase 1: Esquemas y catálogo (RF-1, RF-2, RF-3, RF-11)

- [ ] **B1.1**: `finances_core/schemas/backtest.py` (`BacktestRequest`, `ResolvedStrategy`, `CostModel`, `RunManifest`, `RunSummary`, páginas, `BacktestDispatchResponse`) exportado desde `schemas/__init__.py` — depende de B0.2
  - **Hecho cuando**: tests de validación: `start_ms > end_ms`, `size_fraction` fuera de `(0, 1]`, costes negativos y claves extra (`extra="forbid"`) fallan; valores por defecto de RF-3; `mypy --strict` limpio.
- [ ] **B1.2**: `backtest/catalog.py`: `KINDS` (3 tipos), `CATALOG_VERSION`, `parse_strategy_parameters` (RF-1) — depende de B1.1
  - **Hecho cuando**: tests: `{"kind":"sma_cross","params":{"fast":10,"slow":30}}` válido; `fast >= slow`, `lower >= upper`, `kind` desconocido (lista los válidos), clave extra de primer nivel y parámetro extra fallan con `UnknownKindError`/`InvalidParamsError`; `warmup` de cada tipo coincide con RF-2.
- [ ] **B1.3**: `backtest/strategy_source.py` (envoltorio de `ft_strategy_manager.service.get_strategy`; versión por defecto = última; soft-deleted = inexistente) — depende de B1.2
  - **Hecho cuando**: tests con `strategies.db` temporal: última versión por defecto, versión explícita, `StrategyNotFoundError`/`VersionNotFoundError` mapeadas a 404, soft-deleted → no encontrada, `kind` inválido → error de validación; `ResolvedStrategy.hash` coincide con el de `ft_strategy_manager`.
- [ ] **B1.4**: Guardia AST del núcleo (`test_backtest_isolation.py`) — depende de B1.1
  - **Hecho cuando**: el test falla ante `pandas`, `float64`/`Float64`/`double` o imports `ft_*` (salvo `ft_strategy_manager` en `strategy_source.py`) en `finances_core/backtest/**` y `schemas/backtest.py`, y pasa sobre el código actual (RFC-0004, RF-11).

## Fase 2: Datos (RF-4)

- [ ] **B2.1**: `backtest/candles.py::read_candles` sobre `indicators_store.source` con filtro de rango inclusivo — depende de B1.1
  - **Hecho cuando**: tests con dataset Hive sintético: rango inclusivo, rango por defecto = todo, dtypes `Int64`/`Float32`, dataset inexistente → `SourceNotFoundError`, precio `<= 0`/nulo/`NaN` → `InvalidSourceError`, rango vacío → `EmptyRangeError`, `data_gaps` contado, no se escribe nada bajo `FT_PARQUET_DIR`.

## Fase 3: Costes, tablas canónicas y métricas (RF-5, RF-6, RF-7)

- [ ] **B3.1**: `backtest/costs.py` (`fee_rate`, `slip`, `buy_price`, `sell_price`) y `backtest/frames.py` (esquemas de `trades`/`equity`, `validate_engine_output`) — depende de B1.1
  - **Hecho cuando**: tests: fórmulas de RF-5 con ejemplos a mano; `validate_engine_output` acepta una salida válida y rechaza dtype erróneo, `side = 0`, `exit_reason = 2`, `NaN`, filas desordenadas y `equity.height != n_bars` con `OutputOutOfRangeError`.
- [ ] **B3.2**: `backtest/metrics.py` (RF-7) — depende de B3.1
  - **Hecho cuando**: tests con series de 5–10 barras calculadas a mano: `total_return`, `cagr`, `max_drawdown`, `sharpe`, `sortino`, `win_rate`, `exposure`; `null` en `sharpe` con desviación 0, en `win_rate` sin operaciones; prueba de precisión contra una referencia de `tests/` (tolerancia 1e-3) sobre 1 000 000 de barras; `P` correcto para `1m`, `1h`, `1d`.

## Fase 4: Identidad y almacén (RF-8, RF-9)

- [ ] **B4.1**: `backtest/identity.py` (`data_hash`, `run_id`) — depende de B1.1
  - **Hecho cuando**: tests: vectores fijos (valor esperado literal); cambiar cualquier campo de RF-8 (incluida `engine_version` o una vela) cambia el `run_id`; el orden de claves no influye; `force` y `created_at_ms` no influyen.
- [ ] **B4.2**: `backtest/results_store.py` (`write_run`, `read_manifest`, `list_runs`, `find_run`, `read_trades_page`, `read_equity_page`) — depende de B3.1, B4.1
  - **Hecho cuando**: tests: layout de RF-9; directorio sin `manifest.json` ignorado; escritura concurrente del mismo `run_id` deja un único resultado válido; `force` reemplaza atómicamente con contenido idéntico; temporales huérfanos limpiados; paginación por cursor de `trades`/`equity`; filtros de `list_runs`; Parquet con `zstd` y reescritura byte a byte idéntica.

## Fase 5: Orquestación y contrato de motor (RF-10)

- [ ] **B5.1**: `backtest/engine_api.py` (`BacktestEngine`, `EngineOutput`) y `backtest/runner.py::run_backtest` con fases reportables — depende de B1.3, B2.1, B3.2, B4.2
  - **Hecho cuando**: tests con un motor *fake* determinista: pipeline completo, `cached = true` en la repetición sin invocar al motor, `force` lo invoca, `kind` no soportado → `KindNotSupportedByEngineError`, salida inválida → `OutputOutOfRangeError` sin escribir nada, auditoría `backtest_run` emitida (RF-13).
- [ ] **B5.2**: Configuración `FT_BACKTEST_MAX_CONCURRENT` en `FinancesSettings` (RF-12) — depende de B0.2
  - **Hecho cuando**: `FinancesSettings().BACKTEST_MAX_CONCURRENT == 2`; `FT_BACKTEST_MAX_CONCURRENT=0` y `17` fallan por validación; ver P3 para el registro en `ft_setup`.

## Fase 6: Fixture y suite de contrato (RF-10) — requiere B0.3

- [ ] **B6.1**: Fixture determinista (`PCG64`, ≥ 2 000 barras `1h`, con tendencia y rango, margen mínimo en los umbrales) y referencia de prueba independiente de los 3 tipos (bucle explícito, solo en `tests/`) — depende de B0.3, B1.2
  - **Hecho cuando**: el script `regenerate_golden.py` produce `signals_*.json`, `trades_*.parquet`, `equity_*.parquet`, `summary_*.json` para `sma_cross`, `rsi_reversion` y `donchian_breakout`; cada tipo genera ≥ 5 operaciones; una prueba verifica que ninguna comparación de umbral queda a menos de 1e-3 relativo (sin empates); los *golden* reproducen a mano un caso de 10 barras por tipo.
- [ ] **B6.2**: Suite parametrizable `engine_contract.py` (`assert_engine_matches_golden(engine, kind)`) con las tolerancias de RFC-0004 — depende de B5.1, B6.1
  - **Hecho cuando**: tests de la propia suite: pasa con una implementación de referencia envuelta como motor *fake*; falla (mutación) si se desplaza `entry_time_ms` una barra, si se omite la liquidación final o si se altera una comisión; `exit_reason` y `side` se comparan exactos.

## Fase 7: Cierre del núcleo

- [ ] **B7.1**: Calidad y cobertura del núcleo — depende de B1–B6
  - **Hecho cuando**: `ruff` (check + format), `mypy --strict src` de `finances-core` y `finances-backtester` limpios; `pytest --cov=finances_core.backtest` ≥ 90 % (Art. 7); `test_backtest_isolation.py` verde.

## Cambios en otros documentos (pendientes)

No se editan en este ciclo; cada uno es una tarea a ejecutar con su aprobación.

- [x] **P1**: `docs/constitution.md`: enmendar el Art. 8 (propuesta en RFC-0004, punto 6) y subir a 1.2.0 con historial — tras B0.1
  - **Hecho cuando**: la constitución 1.2.0 contiene el texto aprobado y referencia a RFC-0004.
  - **Hecho**: `docs/constitution.md` v1.2.0: Art. 8 enmendado con el texto del RFC-0004 (punto 6) e historial actualizado, 2026-11-02.
- [ ] **P2**: `docs/constitution.md` Art. 10 nombra paquetes inexistentes (`finances-connectors`, `finances-ml`); alinear con los paquetes reales y con `finances-backtester`
  - **Hecho cuando**: el Art. 10 lista solo paquetes existentes o previstos en el README.
- [ ] **P3**: `packages/finances-core/src/ft_setup/env_list.conf` (y `.j2`, `specs/ft_setup`): registrar `FT_BACKTEST_MAX_CONCURRENT` — tras B5.2
  - **Hecho cuando**: `ft_setup` genera la variable con descripción, ejemplo `2` y `source_tool = "ft_backtester"`; sus tests pasan.
- [ ] **P4**: `README.md`: la fila de `finances-backtester` pasa de *(planificado)* a *(en desarrollo)* con `ft_vectorbt`, y se documentan los comandos tras V7.2
  - **Hecho cuando**: el README describe `ft_vectorbt run` y `/backtest/vectorbt` coherentemente con las specs.
- [ ] **P5**: `specs/finances_api/{spec,plan,tasks}.md`: añadir `ft_vectorbt` (prefijo `/backtest/vectorbt`, paquete `finances-backtester`) a RF-1 y una tarea T12 — tras V5.1
  - **Hecho cuando**: `specs/finances_api` lista el prefijo y T12 está marcada con su evidencia.
- [ ] **P6**: `specs/ft_strategy_manager/spec.md`: nota de que `parameters` puede seguir la forma `{kind, params}` del catálogo (US-2 ya cita `ft_vectorbt`) y de que `finances_core.backtest.strategy_source` es consumidor del servicio — informativo
  - **Hecho cuando**: la nota existe y no contradice RF-1/RF-2.
- [ ] **P7**: `AGENTS.md` §1 («Aislamiento Cruzado»): mencionar que el envoltorio `strategy_source` es el modo de consumir `ft_strategy_manager` — solo si A2 se confirma
  - **Hecho cuando**: AGENTS.md y los tests AST son coherentes.
