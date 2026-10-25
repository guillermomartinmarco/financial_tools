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

- [x] **T5**: Montar router `/market-data/yfinance` (ver `specs/ft_market_data_yfinance` T5.2)
  - **Hecho cuando**: `test_openapi_includes_all_tool_routers` incluye el prefijo `/market-data/yfinance/`.
  - **Hecho**: `create_app()` incluye `finances_downloader.ft_market_data_yfinance.api.router`; `test_openapi_includes_all_tool_routers` incluye `/market-data/yfinance/` y `test_openapi_includes_the_four_yfinance_routes` fija las 4 rutas; ruff/format/mypy limpios, `9 passed`.

- [x] **T6**: Montar router `/indicators` y depender de `finances-analytics` (ver `specs/ft_indicators` T7.1)
  - **Hecho cuando**: `test_openapi_includes_all_tool_routers` incluye el prefijo `/indicators/` y un test fija las 7 rutas de `specs/ft_indicators/plan.md` §2.2.
  - **Hecho**: `pyproject.toml` añade `finances-analytics`; `create_app()` incluye `finances_analytics.ft_indicators.api.router`; `test_openapi_includes_all_tool_routers` incluye `/indicators/`, `test_openapi_includes_the_seven_indicators_routes` fija las 7 rutas y `test_mounted_indicators_router_answers` obtiene las 12 entradas de `GET /indicators/catalog`. `GET /health` no cambia (`indicators_dir_accessible` es opcional en el plan de `ft_indicators` §7 y RF-4 no lo exige).
