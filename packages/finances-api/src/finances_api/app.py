"""App FastAPI única que monta los routers de todas las herramientas (RF-1, RF-3, RF-4).

No es una herramienta ``ft_*``: puede importar los ``router`` públicos de todas sin
violar el Art. 4. Ninguna herramienta debe importar de este paquete (RF-5).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from finances_analytics.ft_indicators.api import router as indicators_router
from finances_analytics.ft_ta_lib.api import router as ta_lib_router
from finances_core.config import FinancesSettings
from finances_core.logging import configure_logging
from finances_downloader.ft_data_viewer.api import router as viewer_router
from finances_downloader.ft_datasets.api import router as datasets_router
from finances_downloader.ft_market_data_binance.api import router as binance_router
from finances_downloader.ft_market_data_yfinance.api import router as yfinance_router
from ft_setup.api import router as setup_router
from pydantic import BaseModel

from finances_api import __version__

_COMPONENT = "finances_api"


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    parquet_dir_accessible: bool


def _parquet_dir_accessible() -> bool:
    parquet_dir = FinancesSettings().PARQUET_DIR
    return parquet_dir.is_dir() and os.access(parquet_dir, os.R_OK)


def _build_lifespan(level: int) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging("api", level, component=_COMPONENT)
        yield

    return lifespan


def create_app(log_level: int = logging.INFO) -> FastAPI:
    """Crea la app con ``/health`` y los routers de todas las herramientas.

    Monta ``ft_setup`` (``/setup``), ``ft_market_data_binance`` (``/market-data/binance``),
    ``ft_market_data_yfinance`` (``/market-data/yfinance``), ``ft_data_viewer`` (``/data``),
    ``ft_datasets`` (``/datasets``), ``ft_indicators`` (``/indicators``) y ``ft_ta_lib``
    (``/ta-lib``), estas dos del paquete ``finances-analytics``.
    """
    app = FastAPI(
        title="financial_tools",
        version=__version__,
        lifespan=_build_lifespan(log_level),
    )

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        accessible = await run_in_threadpool(_parquet_dir_accessible)
        return HealthResponse(status="ok", version=__version__, parquet_dir_accessible=accessible)

    app.include_router(setup_router)
    app.include_router(binance_router)
    app.include_router(yfinance_router)
    app.include_router(viewer_router)
    app.include_router(datasets_router)
    app.include_router(indicators_router)
    app.include_router(ta_lib_router)
    return app
