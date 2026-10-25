# Tasks — `finances-api`

- [x] **T1**: Crear `packages/finances-api` (pyproject, `__init__.py` con `__version__`) e instalarlo editable en `.venv`
  - **Hecho cuando**: `python -c "import finances_api"` funciona y `ruff`/`mypy --strict` pasan sobre el paquete.
  - **Hecho**: `pip install -e packages/finances-api --no-deps`; `ruff check .` → All checks passed; `mypy --strict src` → Success (3 archivos).
- [x] **T2**: `create_app()` con los 3 routers, `lifespan` que configura logging JSON y `GET /health`
  - **Hecho cuando**: Test con `httpx.ASGITransport` verifica `GET /health` → 200 y que el esquema OpenAPI contiene rutas bajo `/setup`, `/market-data/binance` y `/data`.
  - **Hecho**: `test_health_returns_ok_and_version`, `test_openapi_includes_all_tool_routers`, `test_mounted_setup_router_answers`, `test_lifespan_configures_json_logging`.
- [x] **T3**: Comando `ft_serve` (`--host`, `--port`, `--log-level`; exit 3 sin uvicorn)
  - **Hecho cuando**: Test con `CliRunner` verifica `ft_serve --help` y que sin uvicorn sale con código 3 (uvicorn simulado ausente).
  - **Hecho**: `test_ft_serve_help`, `test_ft_serve_without_uvicorn_exits_3`, `test_ft_serve_runs_uvicorn_with_settings_defaults`.
- [x] **T4**: Eliminar `ft_setup/serve.py` y actualizar README ("Inicio Rápido") con `ft_serve`
  - **Hecho cuando**: No quedan referencias a `ft_setup serve` ni a `finances-backtester serve` en el repo.
  - **Hecho**: `ft_setup/serve.py` eliminado (y extra `[service]` de finances-core); README usa `ft_serve`; solo quedan menciones históricas en ADRs/notas de auditoría.
