"""Fixtures compartidas: ningún test escribe en la raíz real del proyecto."""

from __future__ import annotations

import os

# Typer fuerza salida de terminal (códigos ANSI) si detecta GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS;
# se desactiva antes de importar Typer para que los tests vean texto plano en cualquier entorno.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

from pathlib import Path

import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import DatasetIdentifier

from .datasets import JAN_2024_MS, DatasetFactory, write_hive_dataset


@pytest.fixture(autouse=True)
def _isolate_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Toda ruta de ``FinancesSettings`` (PARQUET_DIR, INDICATORS_DIR, ...) cae en ``tmp_path``."""
    monkeypatch.setenv("FT_PROJECT_ROOT", str(tmp_path))


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


@pytest.fixture
def hive_dataset(settings: FinancesSettings) -> DatasetFactory:
    """Fábrica: ``hive_dataset(exchange="yfinance", market_type="equity", ...)``."""

    def _make(
        *,
        exchange: str = "binance",
        market_type: str = "spot",
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        rows: int = 100,
        start_ms: int = JAN_2024_MS,
        seed: int = 7,
    ) -> DatasetIdentifier:
        return write_hive_dataset(
            settings.PARQUET_DIR,
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            timeframe=timeframe,
            rows=rows,
            start_ms=start_ms,
            seed=seed,
        )

    return _make
