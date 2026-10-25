# Tasks — `finances-api`

- [ ] **T1**: Crear `packages/finances-api` (pyproject, `__init__.py` con `__version__`) e instalarlo editable en `.venv`
  - **Hecho cuando**: `python -c "import finances_api"` funciona y `ruff`/`mypy --strict` pasan sobre el paquete.
- [ ] **T2**: `create_app()` con los 3 routers, `lifespan` que configura logging JSON y `GET /health`
  - **Hecho cuando**: Test con `httpx.ASGITransport` verifica `GET /health` → 200 y que el esquema OpenAPI contiene rutas bajo `/setup`, `/market-data/binance` y `/data`.
- [ ] **T3**: Comando `ft_serve` (`--host`, `--port`, `--log-level`; exit 3 sin uvicorn)
  - **Hecho cuando**: Test con `CliRunner` verifica `ft_serve --help` y que sin uvicorn sale con código 3 (uvicorn simulado ausente).
- [ ] **T4**: Eliminar `ft_setup/serve.py` y actualizar README ("Inicio Rápido") con `ft_serve`
  - **Hecho cuando**: No quedan referencias a `ft_setup serve` ni a `finances-backtester serve` en el repo.
