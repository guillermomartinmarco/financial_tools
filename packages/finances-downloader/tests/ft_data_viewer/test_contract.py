"""T7.1: contrato CLI vs API (mismos inputs → mismo JSON). Más T0.x y Art. 4."""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterator
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import (
    AnomalyReport,
    CompareRequest,
    DatasetListResponse,
    DatasetSummary,
    ExportRequest,
)
from typer.testing import CliRunner

from finances_downloader.ft_data_viewer.api import router
from finances_downloader.ft_data_viewer.cli import app

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv, write_dataset

DS = ["-e", "binance", "-m", "spot", "-s", "BTCUSDT", "-t", "1h"]
BASE = "/data/datasets/binance/spot/BTCUSDT/1h"
TOOL_DIR = Path(__file__).resolve().parents[2] / "src" / "finances_downloader" / "ft_data_viewer"


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    logging.basicConfig(force=True, handlers=[logging.NullHandler()])
    structlog.reset_defaults()


@pytest.fixture
def client() -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(router)
    with TestClient(api) as c:
        yield c


@pytest.fixture
def dataset(parquet_dir: Path) -> Path:
    df = make_ohlcv(120, drop=[7, 8, 50])
    return write_dataset(parquet_dir, df)


def _cli(args: list[str]) -> object:
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


@pytest.mark.parametrize(
    ("cli_args", "url"),
    [
        (["list", "-f", "json"], "/data/datasets"),
        (["list", "-s", "BTCUSDT", "-f", "json"], "/data/datasets?symbol=BTCUSDT"),
        (["summary", *DS, "-f", "json"], f"{BASE}/summary"),
        (
            ["head", *DS, "-n", "4", "--cols", "open_time_ms,close", "-f", "json"],
            f"{BASE}/rows?mode=head&n=4&cols=open_time_ms,close",
        ),
        (["tail", *DS, "-n", "3", "-f", "json"], f"{BASE}/rows?mode=tail&n=3"),
        (["gaps", *DS, "--min-gap", "1", "-f", "json"], f"{BASE}/gaps?min_gap_candles=1"),
        (["anomalies", *DS, "-f", "json"], f"{BASE}/anomalies"),
    ],
)
def test_cli_and_api_return_same_json(
    client: TestClient, dataset: Path, cli_args: list[str], url: str
) -> None:
    assert _cli(cli_args) == client.get(url).json()


def test_compare_contract(client: TestClient, parquet_dir: Path, dataset: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(120, drop=[9]), market_type="futures")
    cli = _cli(
        [
            "compare",
            "--exchange-a",
            "binance",
            "--market-a",
            "spot",
            "--symbol-a",
            "BTCUSDT",
            "--timeframe-a",
            "1h",
            "--exchange-b",
            "binance",
            "--market-b",
            "futures",
            "--symbol-b",
            "BTCUSDT",
            "--timeframe-b",
            "1h",
            "--start",
            str(JAN_2024_MS),
            "--end",
            str(JAN_2024_MS + 100 * HOUR_MS),
            "-f",
            "json",
        ]
    )
    body = {
        "dataset_a": {
            "exchange": "binance",
            "market_type": "spot",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
        },
        "dataset_b": {
            "exchange": "binance",
            "market_type": "futures",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
        },
        "start_ms": JAN_2024_MS,
        "end_ms": JAN_2024_MS + 100 * HOUR_MS,
    }
    assert cli == client.post("/data/compare", json=body).json()


def test_export_contract(client: TestClient, dataset: Path) -> None:
    res = CliRunner().invoke(app, ["export", *DS, "-f", "csv", "-o", "-"])
    body = {
        "dataset_id": {
            "exchange": "binance",
            "market_type": "spot",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
        },
        "format": "csv",
    }
    api = client.post(f"{BASE}/export", json=body)
    assert res.stdout.splitlines() == api.text.splitlines()


# --- T0.1 / T0.2 ----------------------------------------------------------------------


def test_schemas_validate_json_examples() -> None:
    ident = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}
    DatasetListResponse.model_validate_json(
        json.dumps(
            {
                "datasets": [
                    {
                        "id": ident,
                        "year_range": [2024, 2025],
                        "month_count": 3,
                        "estimated_rows": 10,
                        "size_bytes": 1,
                        "last_modified_ms": 1,
                        "partition_path": "binance/market_type=spot/symbol=BTCUSDT/timeframe=1h",
                    }
                ],
                "total_count": 1,
                "total_size_bytes": 1,
                "total_rows": 10,
            }
        )
    )
    DatasetSummary.model_validate_json(
        json.dumps(
            {
                "dataset_id": ident,
                "row_count": 1,
                "date_range": [1, 2],
                "columns": [{"name": "close", "dtype": "Float32", "null_count": 0}],
                "gaps_detected": 0,
                "duplicate_timestamps": 0,
                "outlier_count": 0,
            }
        )
    )
    AnomalyReport.model_validate_json(
        json.dumps(
            {
                "dataset_id": ident,
                "duplicate_timestamps": {"count": 0},
                "null_values": {},
                "price_outliers": {},
                "volume_anomalies": {},
            }
        )
    )
    assert ExportRequest.model_validate_json(json.dumps({"dataset_id": ident})).format == "parquet"
    CompareRequest.model_validate_json(json.dumps({"dataset_a": ident, "dataset_b": ident}))
    with pytest.raises(ValueError, match="market_type"):
        ExportRequest.model_validate({"dataset_id": {**ident, "market_type": "options"}})


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FT_DATA_VIEWER_SAMPLE_MAX_ROWS", raising=False)
    monkeypatch.delenv("FT_DATA_VIEWER_GAP_THRESHOLD_MS", raising=False)
    s = FinancesSettings()
    assert s.DATA_VIEWER_SAMPLE_MAX_ROWS == 10_000
    assert s.DATA_VIEWER_GAP_THRESHOLD_MS == 0


# --- Art. 4: aislamiento --------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


@pytest.mark.parametrize(
    "path",
    [*sorted(TOOL_DIR.glob("*.py")), *sorted(Path(__file__).parent.glob("*.py"))],
    ids=lambda p: p.name,
)
def test_no_cross_tool_imports(path: Path) -> None:
    for mod in _imported_modules(path):
        parts = mod.split(".")
        foreign = [p for p in parts if p.startswith("ft_") and p != "ft_data_viewer"]
        assert not foreign, f"{path.name} importa {mod} (Art. 4)"


def test_console_script_entry_point_installed() -> None:
    """T0.4: ``pip install -e`` registra el comando ``ft_data_viewer``."""
    (ep,) = [e for e in entry_points(group="console_scripts") if e.name == "ft_data_viewer"]
    assert ep.value == "finances_downloader.ft_data_viewer.cli:app"
    assert ep.load() is app
