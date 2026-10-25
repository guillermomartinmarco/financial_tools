# Tasks — `ft_setup` (MVP v1.0)

> **Convención**: Cada tarea es atómica, testeable independientemente, y sigue TDD (test primero).
> **Hecho cuando**: criterio técnico verificable (archivo existe, test pasa, lint limpio).
>
> **Nota de ejecución (2026-10-25)**: el repositorio todavía no tiene workspace raíz
> con `uv`, por lo que los criterios que citan `uv sync`/`uv run` se verificaron con
> el equivalente `python -m <tool>` sobre `.venv`.

## Fase 0: Fundación del Paquete `finances-core`

- [x] **T0.1**: Crear `packages/finances-core/pyproject.toml` con deps base + opcionales dev
  - **Hecho cuando**: `uv sync --package finances-core` resuelve sin conflictos; `uv run ruff check packages/finances-core` pasa.
  - **Hecho**: `python -m ruff check packages/finances-core` → *All checks passed*; instalación editable OK (`pip install -e packages/finances-core --no-deps`).
- [x] **T0.2**: Crear `packages/finances-core/src/finances_core/__init__.py` + `config.py` con `FinancesSettings`
  - **Hecho cuando**: `from finances_core.config import FinancesSettings` importa; `FinancesSettings()` instancia con defaults.
  - **Hecho**: verificado en tests (`tests/unit/test_setup_service.py`).
- [x] **T0.3**: Crear `packages/finances-core/src/finances_core/schemas/setup.py` con modelos Pydantic (EnvVarSpec, EnvVarDiagnosis, SetupDoctorResponse, InitTaskResult)
  - **Hecho cuando**: Modelos validan ejemplos JSON; MyPy strict pasa sin errors.
  - **Hecho**: `mypy --strict src` → *no issues found in 12 source files*.
- [x] **T0.4**: Crear `packages/finances-core/src/finances_core/schemas/market_data.py` (esqueleto Art. 5 Constitución)
  - **Hecho cuando**: Archivo existe con `OHLCVSchema` usando `Float32`/`Int64` (Polars dtypes como strings).
  - **Hecho**: `OHLCV_SCHEMA` existente; se normalizó a `StrEnum` y se tipó para `mypy --strict`.

## Fase 1: Logging Dual (Art. 9 Constitución)

- [x] **T1.1**: Implementar `packages/finances-core/src/ft_setup/logging.py` con `configure_logging(mode, level)`
  - **Hecho cuando**: Test unitario verifica que `mode="cli"` usa RichHandler y `mode="api"` usa JSONRenderer; structlog emite en ambos casos.
  - **Hecho**: `tests/unit/test_setup_logging.py` (4 casos) + cobertura 100 % del módulo.
- [ ] **T1.2**: Integrar logging en entrypoint CLI (`cli.py` callback) y API (`api.py` lifespan)
  - **Hecho cuando**: `ft_setup --verbose doctor` muestra DEBUG con Rich; `GET /setup/doctor` loguea JSON con `task_id`, `component="ft_setup"`.
  - **Auditoría 2026-10-25 — reabierta**: `api.py` no tiene lifespan; `create_app()` nunca configura logging (solo lo hace `serve.py`, que no es alcanzable).
  - **Hecho**: callback CLI + `configure_api_logging()` en `serve.py`; el router liga `component`/`task_id` vía `structlog.contextvars` y el processor `_add_service_context` añade `trace_id`/`span_id`.

## Fase 2: `env_list.conf` y Plantillas

- [x] **T2.1**: Crear `packages/finances-core/src/ft_setup/templates/env_list.conf.j2` con todas las variables `FT_*` conocidas (mínimo 15 vars cubriendo dirs, API keys, ports, URLs)
  - **Hecho cuando**: Archivo TOML válido; cada entrada tiene `name`, `description`, `example`, `required`, `var_type`, `secret`.
  - **Hecho**: 27 variables (`ft_setup` 13 + `ft_market_data_binance` 13 + `ft_market_data_yfinance` 1); parseadas con `tomllib` en los tests.
- [x] **T2.2**: Crear `packages/finances-core/src/ft_setup/templates/.env.template.j2` generado desde `env_list.conf.j2`
  - **Hecho cuando**: Template renderiza `.env.template` con comentarios, ejemplos, y placeholders para secrets.
  - **Hecho**: `build_env_template()` agrupa por `source_tool`, documenta descripción/ejemplo/requerido y deja `=""` para secretos. Sin dependencia de Jinja2 (sustitución `{{ sections }}`).
- [x] **T2.3**: Copiar `env_list.conf.j2` → `src/ft_setup/env_list.conf` en build (script o `pyproject.toml` `[tool.setuptools.package-data]`)
  - **Hecho cuando**: `importlib.resources.read_text("ft_setup", "env_list.conf")` retorna TOML parseable.
  - **Hecho**: `[tool.setuptools.package-data]` + `env_list.conf` materializado; `load_env_specs()` lo lee vía `importlib.resources.files`.

## Fase 3: Servicio Compartido (Lógica de Negocio)

- [x] **T3.1**: Implementar `detect_secret(name: str, spec_secret: bool) -> bool` (patrones + flag explícito)
  - **Hecho cuando**: Tests cubren: `FT_API_KEY`→True, `FT_DATA_DIR`→False, `FT_SECRET_TOKEN`→True, flag explícito override.
  - **Hecho**: 8 casos parametrizados + override explícito.
- [x] **T3.2**: Implementar `validate_var(spec: EnvVarSpec, value: Optional[str]) -> tuple[bool, Optional[str]]`
  - **Hecho cuando**: Tests: puerto 70000→False, "not-a-url"→False, path inexistente→False (pero creable→True), host válido→True.
  - **Hecho**: 14 casos parametrizados + rutas creables con `tmp_path`.
- [x] **T3.3**: Implementar `diagnose() -> SetupDoctorResponse` en `service.py`
  - **Hecho cuando**: Test de integración con `monkeypatch.setenv` verifica diagnóstico correcto para: todas presentes, faltantes requeridas, tipos inválidos, secrets enmascarados.
  - **Hecho**: 4 tests (secreto enmascarado, requerida ausente, tipo inválido, fuente `.env`). Los secretos nunca se serializan (`value=None`).
- [x] **T3.4**: Implementar `run_init(force: bool, dry_run: bool) -> InitTaskResult` en `service.py`
  - **Hecho cuando**: Test verifica: directorios creados, archivos generados, tasks.db WAL creado/verificado, idempotencia (2da llamada no duplica), dry_run no escribe disco.
  - **Hecho**: 3 tests (`run_init` idempotente, dry-run, fallo persistido).
- [x] **T3.5**: Integrar `SQLTaskManager` (existente o nuevo) para persistir tarea de `init`
  - **Hecho cuando**: `init` via CLI y API crea registro en `share/tasks.db` con estados correctos; `SELECT * FROM tasks` muestra columnas esperadas.
  - **Hecho**: nuevo gestor canónico `finances_core/tasks.py` (WAL, conexión por llamada, Art. 16); CLI y API lo usan vía `service`.

## Fase 4: CLI (Typer)

- [x] **T4.1**: Implementar `cli.py` con `app`, `main` callback (logging), `init` command, `doctor` command
  - **Hecho cuando**: `uv run finances-core setup --help` muestra comandos; `uv run finances-core setup doctor` imprime tabla Rich bonita.
  - **Hecho**: entry point real `ft_setup` (`[project.scripts]`); `ft_setup --help` y `ft_setup doctor` verificados.
- [ ] **T4.2**: Flags CLI: `--verbose/-v`, `--quiet/-q`, `--log-format [rich|json]`, `--force`, `--dry-run`, `--format [table|json]`
  - **Hecho cuando**: Tests verifican cada flag afecta comportamiento (nivel log, formato salida, force overwrite, dry-run).
  - **Hecho**: `tests/integration/test_setup_cli.py` (7 tests) cubre formato, dry-run, idempotencia y logs.
  - **Auditoría 2026-10-25 — reabierta**: `--force`, `--verbose` y `--log-format` no tienen test a nivel CLI; `doctor` vuelve a llamar `configure_logging` (`cli.py:76`) y pisa el `--log-format json` del callback.
- [ ] **T4.3**: Exit codes: `0` OK, `1` required vars missing, `2` validation errors, `3` IO/perm error
  - **Hecho cuando**: Tests `pytest -k cli_exit_codes` pasan.
  - **Hecho**: cubiertos como `test_doctor_invalid_value_exits_2`, `test_doctor_missing_required_exits_1`, `test_doctor_json_ok`, `test_init_*` (IO → 3).
  - **Auditoría 2026-10-25 — reabierta**: ningún test afirma exit code 3; `test_init_*` solo verifica `EXIT_OK`.

## Fase 5: API (FastAPI)

- [ ] **T5.1**: Implementar `api.py` con router `/setup`, endpoints `GET /doctor`, `POST /init`, `GET /status/{task_id}`
  - **Hecho cuando**: `ft_serve` (paquete `finances-api`) levanta servidor; `curl localhost:8000/setup/doctor` retorna JSON válido vs `SetupDoctorResponse`.
  - **Hecho**: `create_app()` + `ft_setup serve` (extra `[service]` con uvicorn, aún no instalado en `.venv`); contrato verificado con `httpx.ASGITransport`.
  - **Auditoría 2026-10-25 — reabierta**: router y endpoints OK, pero `ft_setup serve` no existe: `serve.py` define su propio Typer app que no está montado en `cli.app` ni declarado en `[project.scripts]` (verificado: "No such command 'serve'").
- [x] **T5.2**: `POST /init` retorna `202 ACCEPTED` con `{task_id, status: "ACCEPTED", poll_url}` (Art. 18)
  - **Hecho cuando**: Test verifica response schema, `task_id` es UUID v7 (regex), `poll_url` absoluta.
  - **Hecho**: `test_init_dispatch_and_poll` valida 202, regex UUID v7 y `poll_url` absoluta.
- [x] **T5.3**: `GET /status/{task_id}` retorna `InitTaskResult` con estado actual
  - **Hecho cuando**: Test polling: POST init → GET status → COMPLETED; 404 si task_id inexistente.
  - **Hecho**: tests de polling, 404 y 422 por `task_id` inválido.

## Fase 6: Tests y Calidad (Art. 7, 17 Constitución)

- [x] **T6.1**: Tests unitarios: `detect_secret`, `validate_var`, `diagnose` (mockeando settings)
  - **Hecho cuando**: `pytest packages/finances-core/tests/unit/test_setup_service.py -v` pasa; cobertura ≥ 90%.
  - **Hecho**: 39 tests unitarios + 13 de integración; cobertura `service.py` 98 %, total `ft_setup` 93 %.
- [x] **T6.2**: Tests de integración CLI: `init`, `doctor` (temp dirs, env vars via monkeypatch)
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_setup_cli.py -v` pasa.
  - **Hecho**: 7 tests, verdes.
- [x] **T6.3**: Tests de integración API: endpoints con `httpx.AsyncClient` (testserver)
  - **Hecho cuando**: `pytest packages/finances-core/tests/integration/test_setup_api.py -v` pasa.
  - **Hecho**: 6 tests, verdes.
- [ ] **T6.4**: Configurar `ruff` + `mypy --strict` en `finances-core`; `pre-commit` hooks
  - **Hecho cuando**: `uv run ruff check packages/finances-core` && `uv run mypy --strict packages/finances-core` → 0 errors.
  - **Hecho**: ruff 0.17.2 sin errores (config migrada a `[tool.ruff.lint]`), `mypy --strict src` limpio; `.pre-commit-config.yaml` añadido en la raíz (pre-commit no instalado aún).
  - **Auditoría 2026-10-25 — reabierta**: desde `packages/finances-core`: `ruff check .` → 32 errores; `mypy --strict src` → 2 errores en `finances_core/schemas/data_viewer.py` (añadido después, con ft_data_viewer).

## Fase 7: Documentación y Cierre

- [ ] **T7.1**: Actualizar `README.md` sección "Inicio Rápido" con `ft_setup init` y `ft_setup doctor`
  - **Hecho cuando**: README refleja comandos reales.
  - **Hecho**: sección 3 reescrita con comandos verificados y nota sobre el workspace `uv` pendiente.
  - **Auditoría 2026-10-25 — reabierta**: `README.md:79` documenta `ft_setup serve`, que no funciona (ver T5.1).
- [ ] **T7.2**: Verificar matriz Art. 20 Constitución (100% dimensiones) via checklist interno
  - **Hecho cuando**: Checklist firmado en PR description; CI pipeline verde.
  - **Pendiente**: no hay pipeline CI en el repositorio todavía.
- [ ] **T7.3**: Resolver `TASKS_DB_PATH` respecto de la raíz del proyecto (no del cwd), según Art. 2
  - **Hecho cuando**: Test ejecuta `SQLTaskManager()` desde un subdirectorio y la base usada es `<raíz>/share/tasks.db`; los tests de API (`test_setup_api.py`) dejan de escribir en bases reales del repo.

---

## Estado de ejecución (2026-10-25)

| Comprobación | Resultado |
|---|---|
| `pytest packages/finances-core/tests -q` | **52 passed** |
| Cobertura (`--cov=ft_setup`) | **93 %** total · `service.py` 98 % |
| `ruff check packages/finances-core` | **0 errores** (al cierre original; **32** en auditoría 2026-10-25) |
| `mypy --strict src` (desde `packages/finances-core`) | **0 errores** (12 archivos) (al cierre original; **2** en 13 archivos en auditoría 2026-10-25) |
| `ft_setup --help` / `doctor` / `init` | Verificados manualmente |

**Desviaciones conscientes registradas**:

1. `env_list.conf` se materializa al instalar/ejecutar `init`, no en un paso de *build* separado (no hay backend de build con hooks en el repo).
2. El render de plantillas usa sustitución `{{ clave }}` en lugar de Jinja2 para no añadir una dependencia no declarada en `plan.md §5`.
3. `ft_setup serve` sustituye al inexistente `finances-backtester serve` (ese paquete aún no existe).
4. `SQLTaskManager` se creó en `finances_core/tasks.py` (plan lo ubicaba en `packages/finances-core/share/tasks.db`); el artefacto de datos sigue siendo `./share/tasks.db` (relativo al cwd; Art. 2 corregido el 2026-10-25 a `<raíz>/share/tasks.db` absoluto → ver T7.3).
