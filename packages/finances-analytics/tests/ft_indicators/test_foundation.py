"""T0.0–T0.2: prerrequisitos del núcleo, metadatos del paquete y esqueleto de ``ft_indicators``."""

from __future__ import annotations

import importlib
import importlib.metadata

import polars as pl
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import DatasetIdentifier
from typer.testing import CliRunner

from finances_analytics.ft_indicators import api, cli

from .conftest import DatasetFactory


def test_core_prerequisites_from_yfinance_branch() -> None:
    """T0.0: núcleo ``marketdata`` extraído y ``market_type`` ampliado."""
    for mod in ("hive_writer", "inventory", "plans", "tasks_lifecycle"):
        importlib.import_module(f"finances_core.marketdata.{mod}")
    ds = DatasetIdentifier(exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")
    assert ds.market_type == "equity"


def test_talib_is_dev_only_dependency() -> None:
    """T0.1: ``ta-lib`` solo en el extra ``dev``; nunca ``finances-downloader`` ni pandas."""
    requires = importlib.metadata.requires("finances-analytics") or []
    talib = [r for r in requires if r.lower().startswith("ta-lib")]
    assert talib, "ta-lib debe declararse en el extra dev"
    assert all('extra == "dev"' in r for r in talib)
    runtime = [r.split(";")[0].strip().lower() for r in requires if "extra ==" not in r]
    assert not any(r.startswith(("finances-downloader", "pandas")) for r in runtime)
    assert any(r.startswith("finances-core") for r in runtime)


def test_talib_test_dependency_importable() -> None:
    """T0.1/T0.4: la rueda binaria de ``ta-lib`` funciona (solo en tests)."""
    talib = importlib.import_module("talib")
    assert str(talib.__version__).startswith("0.8.")


def test_cli_and_router_import() -> None:
    """T0.2: el esqueleto expone ``cli.app`` y ``api.router`` con prefijo ``/indicators``."""
    assert api.router.prefix == "/indicators"
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "ft_indicators" in result.output


def test_factory_writes_readable_binance_dataset(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=50)
    files = sorted((settings.PARQUET_DIR / "binance").rglob("data.parquet"))
    assert files
    df = pl.scan_parquet(files).collect()
    assert df.height == 50
    assert "adj_close" not in df.columns
    assert df.schema["close"] == pl.Float32
    assert ds.exchange == "binance"


def test_factory_writes_yfinance_dataset_with_adj_close(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")
    files = sorted((settings.PARQUET_DIR / "yfinance").rglob("data.parquet"))
    df = pl.scan_parquet(files).collect()
    assert df.height == 100
    assert df.schema["adj_close"] == pl.Float32
    assert ds.market_type == "equity"
