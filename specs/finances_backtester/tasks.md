# Tasks — `finances-backtester` (contratos compartidos, MVP v1.0)

> **Convención**: cada tarea es atómica, testeable y sigue TDD (Art. 7). **Hecho cuando**: criterio técnico verificable.
> Checks por paquete: `python -m ruff check`, `python -m ruff format --check`, `python -m mypy --strict src`, `python -m pytest -q`.
> Las tareas `B*` (núcleo compartido) **preceden** a las `V*` de `specs/ft_vectorbt/tasks.md`; cada tarea indica su dependencia.
> Tests del núcleo en `packages/finances-core/tests/unit/`; sin red y con `FT_PROJECT_ROOT` temporal.
> Estado: 16 de 34 tareas completadas (`B0.1`, `B0.2`, `B0.3`, `B1.1`, `B1.2`, `B1.3`, `B1.4`, `B1.5`, `B1.6`, `B1.7`, `B1.8`, `B1.9`, `B1.10`, `B1.11`, `B2.1`, `P1`); el resto pendiente.

## Fase 0: Gobernanza y esqueleto

- [x] **B0.1**: Aprobar RFC-0004 y la enmienda del Art. 8 (constitución 1.2.0) — **bloquea todo `engine.py`**, no el núcleo
  - **Hecho cuando**: `docs/rfc/RFC-0004-backtest-engines-float64-boundary.md` figura `Aprobado` con firma del Tech Lead (ver P1).
  - **Hecho**: RFC-0004 aprobado por el Tech Lead el 2026-11-02 (Estado `Aprobado`).
- [x] **B0.2**: Revisar y resolver los Supuestos A1–A27 de `spec.md` y aprobar la frontera de seguridad de ADR-B10 (estrategias locales sí, código por API nunca)
  - **Hecho cuando**: cada fila de «Supuestos y Decisiones Abiertas» queda marcada `Confirmado` o `Vetado` (con el cambio aplicado a spec/plan), el Tech Lead firma ADR-B09/ADR-B10 y las specs se firman (Art. 20, Contractual).
  - **Evidencia**: el Tech Lead aceptó las cinco decisiones abiertas (A21–A25) en sesión el 02/11/2026 y firmó el PR #15 (specs, ADR-B09/ADR-B10 y matriz del Art. 20), sin vetar ningún supuesto; todas las filas A1–A27 quedan **Confirmadas** tal como están escritas.
- [x] **B0.3**: Crear `packages/finances-backtester` (pyproject según plan §4, `src/finances_backtester/__init__.py` con `__version__`, `py.typed`, `tests/`) sin dependencias de motor — depende de B0.2
  - **Hecho cuando**: `pip install -e "packages/finances-backtester[dev]"` funciona, `python -c "import finances_backtester"` pasa y `ruff`/`mypy --strict src` son verdes.
  - **Evidencia**: local (Python 3.14, con `--ignore-requires-python` porque la cota `<3.14` de A16 lo rechaza): importa, `ruff check`/`ruff format --check`/`mypy --strict src` limpios y 1 test verde. El paquete se añadió a la instalación y al bucle de mypy/pytest de `.github/workflows/ci.yml`; la verificación en Python 3.12 (Windows y Ubuntu) la da el CI de este PR.

## Fase 1: Esquemas, contrato de estrategias y estrategias integradas (RF-1, RF-2, RF-3, RF-11, RF-15…RF-22)

- [x] **B1.1**: `finances_core/schemas/backtest.py` (`BacktestRequest`, `ResolvedStrategy` con `kind_version`/`code_hash`/`origin`, `KindInfo`, `CostModel`, `RunManifest`, `RunSummary`, páginas, `BacktestDispatchResponse`) exportado desde `schemas/__init__.py` — depende de B0.2
  - **Hecho cuando**: tests de validación: `start_ms > end_ms`, `size_fraction` fuera de `(0, 1]`, costes negativos y claves extra (`extra="forbid"`, incluidos `code`, `path`, `module`, `strategies_dir`) fallan; valores por defecto de RF-3; `mypy --strict` limpio.
  - **Evidencia**: `test_backtest_schemas.py` (40 tests) verde, `mypy --strict` limpio y los 13 modelos exportados desde `schemas/__init__.py`.
- [x] **B1.2**: Contrato de estrategia: `strategies/base.py` (`BaseStrategy`, `Signals`, `MetricsTarget`) y `strategies/signals.py::finalize` (RF-16) — depende de B1.1
  - **Hecho cuando**: tests: `finalize` rechaza dtype no `Boolean`, longitud distinta y nulos en `t ≥ warmup` con `InvalidSignalsError`; convierte nulos y `t < warmup` en `false`; anula `entry` y `exit` simultáneas; una subclase sin `signals`, `warmup` o `Params` no se puede instanciar/registrar; `MetricsTarget` rechaza una métrica fuera de RF-7; `mypy --strict` limpio con un `Params` genérico.
  - **Evidencia**: `test_backtest_strategy_contract.py` (35 tests) en verde; `mypy --strict` limpio sobre `src` y sobre el propio test (subclase `BaseStrategy[DemoParams]`).
- [x] **B1.3**: Verificaciones estáticas: `strategies/checks.py` (estructura de carpeta, secciones y tabla del README, guarda AST) y `strategies/hashing.py::code_hash` (RF-11, RF-17, RF-18, RF-19) — depende de B1.2
  - **Hecho cuando**: tests con carpetas temporales: falta `logic.py` o `README.md`, sección ausente/vacía, orden de secciones erróneo, sin bloque `mermaid`, tabla de parámetros que no coincide con `Params`, `kind` ≠ carpeta → error con archivo y causa; la guarda AST rechaza `pandas`, `numpy`, `float64`, `os`, `subprocess`, `socket`, `importlib`, `eval`, `exec`, `open`, `__import__` y acepta `polars`, `pydantic`, `math`, imports relativos hermanos y `finances_core.backtest.strategies`; `code_hash` con vectores fijos: independiente de CRLF/LF, no cambia al editar `README.md`, cambia al editar o añadir un `*.py`, estable entre Windows y Ubuntu.
  - **Evidencia**: `test_backtest_strategy_checks.py` y `test_backtest_strategy_hashing.py` (56 tests) en verde en Windows; vector literal fijo y rutas POSIX (la verificación en Ubuntu la da el CI).
- [x] **B1.4**: Descubrimiento y registro: `strategies/discovery.py` y `backtest/catalog.py` (`StrategyRegistry`, `get_registry`, `parse_strategy_parameters`, `CATALOG_VERSION`) (RF-1, RF-18) — depende de B1.3
  - **Hecho cuando**: tests con `FT_STRATEGIES_DIR` temporal: integradas + locales en orden `kind` ascendente independiente del orden del sistema de archivos; carpetas `_*` y `.*` ignoradas; colisión de nombre → la local `invalid` con `kind_collision` y la integrada intacta; carpeta incompleta o con excepción al importar → `invalid` con mensaje legible (sin traza) y el resto disponible; integrada inválida → `StrategyInitError` al construir; `unknown_kind` lista los válidos; `strategy_init_error` para una local inválida; `invalid_params` por campo; `KindInfo.params_schema` coincide con `Params.model_json_schema()` y no existe ningún `schema.json`; sin recarga en caliente (segunda llamada de `get_registry` devuelve la misma instancia); `FT_STRATEGIES_DIR` no se acepta desde ninguna petición.
  - **Evidencia**: `test_backtest_catalog.py` (34 tests) en verde; las integradas se simulan con un directorio temporal en `build_registry` porque aún no hay ninguna (B1.7).
- [x] **B1.5**: `backtest/strategy_source.py` (envoltorio de `ft_strategy_manager.service.get_strategy`; versión por defecto = última; soft-deleted = inexistente) — depende de B1.4
  - **Hecho cuando**: tests con `strategies.db` temporal: última versión por defecto, versión explícita, `StrategyNotFoundError`/`VersionNotFoundError` mapeadas a 404, soft-deleted → no encontrada, `kind` inválido → error de validación; `ResolvedStrategy.hash` coincide con el de `ft_strategy_manager`; `kind_version`, `code_hash` y `origin` provienen del registro.
  - **Evidencia**: `test_backtest_strategy_source.py` (11 tests) en verde sobre `strategies.db` temporal creada con `ft_strategy_manager`.
- [x] **B1.6**: Verificación de autor: `strategies/testing.py::assert_strategy_contract` y anti look-ahead (RF-20) — depende de B1.2, B1.4
  - **Hecho cuando**: tests: una estrategia sintética válida pasa; una con `shift(-1)` **falla** el anti look-ahead; una que devuelve dtype erróneo, muta `candles` o no es determinista falla; los cortes `{warmup, warmup+1, n/4, n/2, 3n/4, n-1}` se evalúan y los inválidos se descartan sin error.
  - **Evidencia**: `test_strategies_testing.py` (20 tests) en verde; un espía confirma que se evalúan exactamente los cortes válidos.
- [x] **B1.7**: Estrategia integrada `sma_cross` (`strategies/sma_cross/{logic.py,README.md}`) y `strategies/_lib` (`sma`, `cross_above`, `cross_below`) (RF-2, RF-17) — depende de B1.6
  - **Hecho cuando**: casos de 10 barras calculados a mano (calentamiento sin señal, cruce estricto, un empate no genera señal, `entry`/`exit` correctas); `assert_strategy_contract("sma_cross")` pasa; `fast >= slow` → `invalid_params`; README con las 4 secciones, diagrama Mermaid y tabla igual a `Params`.
  - **Evidencia**: `test_strategies_sma_cross.py` (11 tests) y los de `sma`/`cross_*` de `test_strategies_lib.py` en verde (RED observado antes de implementar); defectos `fast=20, slow=100`.
- [x] **B1.8**: Estrategia integrada `rsi_reversion` (RSI de Wilder con semilla SMA; casos 100/50) y `_lib.rsi_wilder` (RF-2, RF-17) — depende de B1.7
  - **Hecho cuando**: casos a mano (semilla en `t = period`, recursión de Wilder, serie plana → RSI 50 sin señal, solo ganancias → RSI 100); `lower >= upper` → `invalid_params`; `assert_strategy_contract("rsi_reversion")` pasa; README completo.
  - **Evidencia**: `test_strategies_rsi_reversion.py` (13 tests) y los de `rsi_wilder` de `test_strategies_lib.py` (semilla, recursión, 50, 100, 0) en verde tras RED; `rsi_wilder` usa `ewm_mean(alpha=1/period, adjust=False)` sembrada con la SMA; defectos `14/30/70`.
- [x] **B1.9**: Estrategia integrada `donchian_breakout` y `_lib.rolling_max_prev`/`rolling_min_prev` (RF-2, RF-17) — depende de B1.7
  - **Hecho cuando**: caso a mano donde incluir la barra actual en el canal cambiaría el resultado; calentamiento `t < window` sin señal; `assert_strategy_contract("donchian_breakout")` pasa; README completo.
  - **Evidencia**: `test_strategies_donchian_breakout.py` (8 tests) y `rolling_*_prev` de `test_strategies_lib.py` en verde tras RED; defecto `window=20`.
- [x] **B1.10**: Guardia AST del núcleo (`test_backtest_isolation.py`) ampliada a `strategies/` — depende de B1.1, B1.9
  - **Hecho cuando**: el test falla ante `pandas`, NumPy, `float64`/`Float64`/`double` o imports `ft_*` (salvo `ft_strategy_manager` en `strategy_source.py`) en `finances_core/backtest/**` (incluidas las estrategias integradas) y `schemas/backtest.py`, y pasa sobre el código actual (RFC-0004, RF-11).
  - **Evidencia**: `test_backtest_isolation.py` (25 tests): 21 fuentes sintéticas infractoras detectadas (imports, nombres, atributos y alias; `ft_strategy_manager` solo en `strategy_source.py`) y 0 infracciones en los módulos reales; las constantes de texto de `checks.py` no se analizan.
- [x] **B1.11**: Plantilla `strategies/_template/` y guía `docs/guides/strategy-authoring.md` (RF-22) — depende de B1.9
  - **Hecho cuando**: la guía contiene los pasos numerados, el checklist, el boilerplate, los ejemplos y la advertencia de seguridad de RF-22; un test copia `_template` a un `FT_STRATEGIES_DIR` temporal, la renombra y `assert_strategy_contract` pasa; otro ejecuta los comandos/ejemplos de la guía contra el directorio temporal; el README raíz enlaza la guía (ver P4).
  - **Nota (verificación de B1.1–B1.6)**: la guía debe advertir que los *junctions*/enlaces simbólicos dentro de `FT_STRATEGIES_DIR` se siguen (cargan código de fuera del directorio), dentro de la frontera de confianza de A23.
  - **Avance**: plantilla, guía (con ambas advertencias) y `test_strategies_authoring.py` (8 tests: plantilla copiada y renombrada, guía ejecutada tal cual con `ft_strategy_manager create`, *package data* declarado en `pyproject.toml`) en verde. **Pendiente**: el enlace desde el README raíz (P4); los comandos de `ft_vectorbt` de la guía no se ejecutan hasta que existan.
  - **Evidencia (cierre)**: el README raíz enlaza la guía; una wheel real de `finances-core` (no editable) contiene las 3 integradas, sus README, `_lib` y `_template`, y desde ella `get_registry` descubre las 3 y `assert_strategy_contract` pasa en todas.

## Fase 2: Datos (RF-4)

- [x] **B2.1**: `backtest/candles.py::read_candles` sobre `indicators_store.source` con filtro de rango inclusivo — depende de B1.1
  - **Hecho cuando**: tests con dataset Hive sintético: rango inclusivo, rango por defecto = todo, dtypes `Int64`/`Float32`, dataset inexistente → `SourceNotFoundError`, precio `<= 0`/nulo/`NaN` → `InvalidSourceError`, rango vacío → `EmptyRangeError`, `data_gaps` contado, no se escribe nada bajo `FT_PARQUET_DIR`.
  - **Nota (verificación de B1.1–B1.6)**: `DatasetIdentifier` acepta `symbol="../../etc"` o `exchange=".."` (ya en la base); `read_candles` debe validar cada componente antes de construir la ruta y rechazar el *path traversal* con un test.
  - **Evidencia**: `test_backtest_candles.py` (27) y +46 casos en `test_backtest_schemas.py` verdes (RED: módulo inexistente y 40 casos de *traversal*). Rango inclusivo, rango por defecto, dtypes, `SourceNotFoundError`, `InvalidSourceError` (precio `<= 0`/nulo/`NaN`/`inf`, orden), `EmptyRangeError` y `data_gaps`; el árbol de `FT_PARQUET_DIR` queda idéntico tras leer. *Path traversal* en dos capas: `BacktestDataset` valida cada componente y `read_candles` revalida y exige que la ruta resuelta quede bajo `PARQUET_DIR`. Los precios se validan solo dentro del rango pedido; orden y dtypes sobre toda la serie.

## Fase 3: Costes, tablas canónicas y métricas (RF-5, RF-6, RF-7)

- [ ] **B3.1**: `backtest/costs.py` (`fee_rate`, `slip`, `buy_price`, `sell_price`) y `backtest/frames.py` (esquemas de `trades`/`equity`, `validate_engine_output`) — depende de B1.1
  - **Hecho cuando**: tests: fórmulas de RF-5 con ejemplos a mano; `validate_engine_output` acepta una salida válida y rechaza dtype erróneo, `side = 0`, `exit_reason = 2`, `NaN`, filas desordenadas y `equity.height != n_bars` con `OutputOutOfRangeError`.
- [ ] **B3.2**: `backtest/metrics.py` (RF-7) — depende de B3.1
  - **Hecho cuando**: tests con series de 5–10 barras calculadas a mano: `total_return`, `cagr`, `max_drawdown`, `sharpe`, `sortino`, `win_rate`, `exposure`; `null` en `sharpe` con desviación 0, en `win_rate` sin operaciones; prueba de precisión contra una referencia de `tests/` (tolerancia 1e-3) sobre 1 000 000 de barras; `P` correcto para `1m`, `1h`, `1d`.

## Fase 4: Identidad y almacén (RF-8, RF-9)

- [ ] **B4.1**: `backtest/identity.py` (`data_hash`, `run_id`) — depende de B1.1
  - **Hecho cuando**: tests: vectores fijos (valor esperado literal); cambiar cualquier campo de RF-8 (incluida `engine_version`, una vela, `kind_version` o `strategy_code_hash`) cambia el `run_id`; el orden de claves no influye; `force` y `created_at_ms` no influyen.
- [ ] **B4.2**: `backtest/results_store.py` (`write_run`, `read_manifest`, `list_runs`, `find_run`, `read_trades_page`, `read_equity_page`) — depende de B3.1, B4.1
  - **Hecho cuando**: tests: layout de RF-9; directorio sin `manifest.json` ignorado; escritura concurrente del mismo `run_id` deja un único resultado válido; `force` reemplaza atómicamente con contenido idéntico; temporales huérfanos limpiados; paginación por cursor de `trades`/`equity`; filtros de `list_runs`; Parquet con `zstd` y reescritura byte a byte idéntica; el manifiesto contiene `kind_version`, `strategy_code_hash` y `strategy_origin`.

## Fase 5: Orquestación y contrato de motor (RF-10, RF-21)

- [ ] **B5.1**: `backtest/engine_api.py` (`BacktestEngine` sin `supported_kinds`, `EngineOutput`) y `backtest/runner.py::run_backtest` (cálculo y normalización de señales tras la comprobación de caché) con fases reportables — depende de B1.4, B1.5, B2.1, B3.2, B4.2
  - **Hecho cuando**: tests con un motor *fake* determinista: pipeline completo, `cached = true` en la repetición sin invocar al motor **ni calcular señales**, `force` los invoca, editar el código de una estrategia local cambia el `run_id` (sin `cached`), `invalid_signals` y `strategy_runtime_error` → tarea `FAILED` sin escribir nada, `strategy_init_error` para una local inválida, salida inválida → `OutputOutOfRangeError` sin escribir nada, auditoría `backtest_run` con `kind_version`/`strategy_code_hash`/`strategy_origin`/`signals_ms` (RF-13); un test inspecciona que `BacktestEngine.run` no tiene parámetros `kind`/`params`/estrategia (RF-21).
- [ ] **B5.2**: Configuración `FT_BACKTEST_MAX_CONCURRENT` en `FinancesSettings` (RF-12) y resolución absoluta de `FT_STRATEGIES_DIR` respecto de la raíz del proyecto (A21) — depende de B0.2
  - **Hecho cuando**: `FinancesSettings().BACKTEST_MAX_CONCURRENT == 2`; `FT_BACKTEST_MAX_CONCURRENT=0` y `17` fallan por validación; `FT_STRATEGIES_DIR` relativo se resuelve contra `FT_PROJECT_ROOT`, no contra el directorio de trabajo; ver P3 para el registro en `ft_setup`.

## Fase 6: Fixture y suite de contrato (RF-10, RF-21) — requiere B0.3

- [ ] **B6.1**: Fixture determinista (`PCG64`, ≥ 2 000 barras `1h`, con tendencia y rango, margen mínimo en los umbrales) y referencia de prueba independiente de las 3 estrategias (bucle explícito, solo en `tests/`) — depende de B0.3, B1.4
  - **Hecho cuando**: el script `regenerate_golden.py` produce `signals_*.json`, `trades_*.parquet`, `equity_*.parquet`, `summary_*.json` para `sma_cross`, `rsi_reversion` y `donchian_breakout`; cada estrategia genera ≥ 5 operaciones; una prueba verifica que ninguna comparación de umbral queda a menos de 1e-3 relativo (sin empates); los *golden* reproducen a mano un caso de 10 barras por estrategia.
- [ ] **B6.2**: Suite parametrizable `engine_contract.py` (`assert_engine_matches_golden(engine, kind)`: obtiene las señales del núcleo y llama a `engine.run`) con las tolerancias de RFC-0004 y la estrategia sintética de RF-21 — depende de B5.1, B6.1, B1.9
  - **Hecho cuando**: tests de la propia suite: pasa con una implementación de referencia envuelta como motor *fake*; falla (mutación) si se desplaza `entry_time_ms` una barra, si se omite la liquidación final o si se altera una comisión; `exit_reason` y `side` se comparan exactos; una estrategia sintética de prueba (no integrada) con señales arbitrarias pasa por el mismo motor *fake* sin código específico; un test AST verifica que ningún módulo de motor importa `finances_core.backtest.strategies`/`catalog` (RF-15).
- [ ] **B6.3**: `test_signals_golden.py`: las señales del núcleo para las 3 estrategias coinciden con `signals_*.json` — depende de B1.9, B6.1
  - **Hecho cuando**: `entries`/`exits` iguales bit a bit a los *golden* sobre el *fixture*; si la recursión `Float32` de Wilder (`rsi_reversion`) difiere de la referencia en doble precisión en algún umbral, la diferencia queda registrada en este archivo con la barra y el valor (A6) y se corrige el *fixture* o se justifica.

## Fase 7: Cierre del núcleo

- [ ] **B7.1**: Calidad y cobertura del núcleo — depende de B1–B6
  - **Hecho cuando**: `ruff` (check + format), `mypy --strict src` de `finances-core` y `finances-backtester` limpios; `pytest --cov=finances_core.backtest` ≥ 90 % (Art. 7); `test_backtest_isolation.py` verde; `assert_strategy_contract` verde para las 3 integradas.

## Cambios en otros documentos (pendientes)

No se editan en este ciclo; cada uno es una tarea a ejecutar con su aprobación.

- [x] **P1**: `docs/constitution.md`: enmendar el Art. 8 (propuesta en RFC-0004, punto 6) y subir a 1.2.0 con historial — tras B0.1
  - **Hecho cuando**: la constitución 1.2.0 contiene el texto aprobado y referencia a RFC-0004.
  - **Hecho**: `docs/constitution.md` v1.2.0: Art. 8 enmendado con el texto del RFC-0004 (punto 6) e historial actualizado, 2026-11-02.
- [ ] **P2**: `docs/constitution.md` Art. 10 nombra paquetes inexistentes (`finances-connectors`, `finances-ml`); alinear con los paquetes reales y con `finances-backtester`
  - **Hecho cuando**: el Art. 10 lista solo paquetes existentes o previstos en el README.
- [ ] **P3**: `packages/finances-core/src/ft_setup/env_list.conf` (y `.j2`, `specs/ft_setup`): registrar `FT_BACKTEST_MAX_CONCURRENT` y actualizar la descripción de `FT_STRATEGIES_DIR` («Raíz de las estrategias Python locales: una subcarpeta por `kind` con `logic.py` y `README.md`; el código se ejecuta con los privilegios del servicio») — tras B5.2
  - **Hecho cuando**: `ft_setup` genera ambas variables con descripción, ejemplo y `source_tool = "ft_backtester"` (la primera) / descripción nueva (la segunda); sus tests pasan.
- [ ] **P4**: `README.md`: la fila de `finances-backtester` pasa de *(planificado)* a *(en desarrollo)* con `ft_vectorbt`, se documentan los comandos tras V7.2 y se enlaza `docs/guides/strategy-authoring.md`
  - **Hecho cuando**: el README describe `ft_vectorbt run`, `/backtest/vectorbt` y cómo añadir una estrategia local, coherentemente con las specs.
- [ ] **P5**: `specs/finances_api/{spec,plan,tasks}.md`: añadir `ft_vectorbt` (prefijo `/backtest/vectorbt`, paquete `finances-backtester`) a RF-1 y una tarea T12 — tras V5.1
  - **Hecho cuando**: `specs/finances_api` lista el prefijo y T12 está marcada con su evidencia.
- [ ] **P6**: `specs/ft_strategy_manager/spec.md`: nota de que `parameters` puede seguir la forma `{kind, params}` del registro de estrategias (integradas y locales; US-2 ya cita `ft_vectorbt`) y de que `finances_core.backtest.strategy_source` es consumidor del servicio — informativo
  - **Hecho cuando**: la nota existe y no contradice RF-1/RF-2.
- [ ] **P7**: `AGENTS.md` §1 («Aislamiento Cruzado») y §4: mencionar que el envoltorio `strategy_source` es el modo de consumir `ft_strategy_manager` (solo si A2 se confirma) y que las estrategias Python viven en `finances_core/backtest/strategies/` y `FT_STRATEGIES_DIR`, que las señales se calculan en el núcleo y que **nunca se acepta código de estrategia por la API**
  - **Hecho cuando**: AGENTS.md y los tests AST son coherentes.
- [ ] **P8**: *(opcional, decisión del Tech Lead)* `docs/constitution.md`: evaluar si la frontera «ningún código se carga por red» merece un artículo propio o basta con ADR-B10 y RF-18 — informativo, no bloquea
  - **Hecho cuando**: decisión registrada (artículo añadido con RFC, o rechazada con motivo) en este archivo.
- [ ] **P9**: *(futuro, no bloquea; ADR-B13)* Paridad CLI/API de `check_strategy`: exponer `POST /kinds/{kind}/check` con política de ejecución `static` (sin importar ni ejecutar código del autor) y, tras un RFC de aislamiento en subproceso (A23), con política `isolated` — tras B1.x y el RFC de aislamiento
  - **Hecho cuando**: tests de contrato CLI↔API sobre el mismo `CheckReport`; la política `static` responde sin importar el módulo (espía) y marca `dynamic_checks: "skipped"`; un test AST confirma que `api.py` no importa módulos de estrategias locales; la política `isolated` aplica límites de tiempo, memoria y red y solo se habilita con el RFC aprobado.
