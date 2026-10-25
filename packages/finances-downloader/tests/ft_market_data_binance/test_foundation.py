"""T0.2-T0.4 y Art. 4 — fundación del paquete, contratos y aislamiento de módulos."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from finances_core import schemas
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data import DownloadPlan

import finances_downloader.ft_market_data_binance as tool
from finances_downloader.ft_market_data_binance import models

SPEC_PLAN_EXAMPLE = {
    "task_id": "uuid-v7",
    "created_at_ms": 1700000000000,
    "items": [
        {
            "source": "public_data",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "market_type": "spot",
            "url": "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip",
            "expected_checksum": "sha256:...",
            "covers_range_ms": [1704067200000, 1706745600000],
        },
        {
            "source": "rest_api",
            "symbol": "ETHUSDT",
            "timeframe": "5m",
            "market_type": "futures",
            "endpoint": "/fapi/v1/klines",
            "params": {
                "symbol": "ETHUSDT",
                "interval": "5m",
                "startTime": 1700000000000,
                "endTime": 1700086400000,
                "limit": 1000,
            },
            "covers_range_ms": [1700000000000, 1700086400000],
        },
    ],
}


def test_cli_module_imports() -> None:
    assert importlib.import_module("finances_downloader.ft_market_data_binance.cli").app


def test_models_reexport_core_contracts() -> None:
    assert models.DownloadPlan is schemas.market_data.DownloadPlan
    assert set(models.__all__) >= {"DownloadRequest", "DownloadResult", "OHLCV_SCHEMA"}


def test_spec_plan_json_example_validates() -> None:
    plan = DownloadPlan.model_validate(SPEC_PLAN_EXAMPLE)
    assert [i.source for i in plan.items] == ["public_data", "rest_api"]


def test_settings_binance_defaults() -> None:
    s = FinancesSettings()
    assert s.model_config.get("env_prefix") == "FT_"
    assert s.BINANCE_PUBLIC_DATA_BASE_URL_SPOT == "https://data.binance.vision/data/spot"
    assert s.BINANCE_PUBLIC_DATA_BASE_URL_FUTURES == "https://data.binance.vision/data/futures/um"
    assert (s.BINANCE_PUBLIC_DATA_RATE_CAPACITY, s.BINANCE_PUBLIC_DATA_RATE_REFILL_RATE) == (
        50,
        10.0,
    )
    assert s.BINANCE_REST_BASE_URL_SPOT == "https://api.binance.com"
    assert s.BINANCE_REST_BASE_URL_FUTURES == "https://fapi.binance.com"
    assert (s.BINANCE_REST_RATE_CAPACITY, s.BINANCE_REST_RATE_REFILL_RATE) == (1200, 20.0)
    assert (s.BINANCE_MAX_RETRIES, s.BINANCE_BASE_BACKOFF_MS) == (3, 1000)
    assert s.DOWNLOAD_MAX_CONCURRENT == 5
    assert s.PARQUET_DIR.is_absolute()


def test_no_cross_imports_to_sibling_tools() -> None:
    """Art. 4: sólo ``finances_core`` y el propio paquete de la herramienta."""
    offenders: list[str] = []
    for source in Path(tool.__file__).parent.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith("finances_downloader.") and not name.startswith(
                    "finances_downloader.ft_market_data_binance"
                ):
                    offenders.append(f"{source.name}: {name}")
    assert offenders == []


def test_tool_has_no_own_logging_module() -> None:
    assert not (Path(tool.__file__).parent / "logging.py").exists()
