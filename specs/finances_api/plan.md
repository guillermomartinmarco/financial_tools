# Plan — `finances-api`

## 1. Contratos de datos (Pydantic)
```python
class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    parquet_dir_accessible: bool
```

## 2. Rutas FastAPI — `packages/finances-api/src/finances_api/app.py`
| Método | Ruta | Respuesta | Origen |
|---|---|---|---|
| GET | `/health` | `HealthResponse` | finances_api |
| * | `/setup/*` | ver `specs/ft_setup/plan.md` | `ft_setup.api.router` |
| * | `/market-data/binance/*` | ver `specs/ft_market_data_binance/plan.md` | `finances_downloader.ft_market_data_binance.api.router` |
| * | `/data/*` | ver `specs/ft_data_viewer/plan.md` | `finances_downloader.ft_data_viewer.api.router` |
| * | `/market-data/yfinance/*` | ver `specs/ft_market_data_yfinance/plan.md` | `finances_downloader.ft_market_data_yfinance.api.router` |
| * | `/indicators/*` | ver `specs/ft_indicators/plan.md` §2.2 | `finances_analytics.ft_indicators.api.router` |

```python
def create_app() -> FastAPI:
    app = FastAPI(title="financial_tools", version=__version__, lifespan=lifespan)
    app.include_router(setup_router)
    app.include_router(binance_router)
    app.include_router(viewer_router)
    app.include_router(yfinance_router)
    app.include_router(indicators_router)
    return app
```

## 3. CLI (Typer) — `packages/finances-api/src/finances_api/cli.py`
```
ft_serve [--host TEXT] [--port INT] [--log-level TEXT]
```
Entry point: `[project.scripts] ft_serve = "finances_api.cli:app"`.

## 4. Estructura
```
packages/finances-api/
├── pyproject.toml          # deps: finances-core, finances-downloader, finances-analytics, fastapi; extra [service]=uvicorn
├── src/finances_api/
│   ├── __init__.py         # __version__
│   ├── app.py              # create_app, lifespan, /health
│   └── cli.py              # ft_serve
└── tests/
    └── test_app.py
```

## 5. ADRs
| ID | Decisión | Motivo |
|---|---|---|
| ADR-API01 | Paquete de composición separado en lugar de un `serve` por herramienta | Un solo host/puerto y un único `/docs`; mantiene el Art. 4 porque no es una herramienta `ft_*` |
| ADR-API02 | `ft_setup serve` se elimina; `ft_serve` lo reemplaza | Evita dos entrypoints de servicio con comportamiento distinto |
