"""T0.0–T0.2: prerrequisitos del núcleo, dependencias y esqueleto de ``ft_ta_lib``."""

from __future__ import annotations

import importlib
import importlib.metadata
from pathlib import Path

import polars as pl
import pytest
from finances_core.indicators_store.namespaces import NamespaceViolationError
from finances_core.indicators_store.store import IndicatorStore
from finances_core.schemas.data_viewer import DatasetIdentifier
from typer.testing import CliRunner

from finances_analytics.ft_ta_lib import api, cli

from .conftest import DatasetFactory, ohlcv_frame


def test_core_indicators_store_is_available() -> None:
    """T0.0: el núcleo compartido de ``ft_indicators`` está en ``main``."""
    for mod in ("store", "manifest", "view", "runner", "request", "source"):
        importlib.import_module(f"finances_core.indicators_store.{mod}")


def test_store_accepts_talib_ids_and_rejects_the_rest(
    tmp_path: Path, hive_dataset: DatasetFactory
) -> None:
    """T0.0: ``IndicatorStore(root, "ft_ta_lib")`` acepta ``talib_*`` y rechaza los demás."""
    ds = hive_dataset(rows=5)
    store = IndicatorStore(tmp_path / "ind", "ft_ta_lib")
    frame = ohlcv_frame(5).select("open_time_ms", pl.col("close").alias("talib_sma_2"))
    stats = store.write_series(ds, "talib_sma_2", frame, {"talib_sma_2": "Float32"})
    assert stats.rows == 5
    bad = frame.rename({"talib_sma_2": "sma_2"})
    for sid in ("sma_2", "talib_", "TALIB_SMA"):
        with pytest.raises(NamespaceViolationError):
            store.write_series(ds, sid, bad, {"sma_2": "Float32"})


def test_talib_is_a_runtime_dependency_with_numpy() -> None:
    """T0.1: ``ta-lib>=0.8.2,<0.9`` y ``numpy`` en *runtime*; sin pandas ni descargadores."""
    requires = importlib.metadata.requires("finances-analytics") or []
    runtime = [r.split(";")[0].strip().lower() for r in requires if "extra ==" not in r]
    talib = [r for r in runtime if r.startswith("ta-lib")]
    assert len(talib) == 1
    assert ">=0.8.2" in talib[0]
    assert "<0.9" in talib[0]
    assert any(r.startswith("numpy") for r in runtime)
    assert not any(r.startswith(("finances-downloader", "pandas")) for r in runtime)


def test_ft_ta_lib_console_script_is_declared() -> None:
    """T0.1: script ``ft_ta_lib = finances_analytics.ft_ta_lib.cli:app``."""
    eps = importlib.metadata.entry_points(group="console_scripts")
    target = {ep.name: ep.value for ep in eps}.get("ft_ta_lib")
    assert target == "finances_analytics.ft_ta_lib.cli:app"


def test_talib_binary_wheel_imports() -> None:
    talib = importlib.import_module("talib")
    assert str(talib.__version__).startswith("0.8.")


def test_cli_and_router_import() -> None:
    """T0.2: el esqueleto expone ``cli.app`` y ``api.router`` con prefijo ``/ta-lib``."""
    assert api.router.prefix == "/ta-lib"
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "ft_ta_lib" in result.output


def test_shared_factory_writes_a_readable_dataset(hive_dataset: DatasetFactory) -> None:
    ds = hive_dataset(exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")
    assert ds == DatasetIdentifier(
        exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d"
    )
