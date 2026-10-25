"""T5.5: contrato CLI ≡ API (Art. 1): mismo JSON y misma petición canónica."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import IndicatorComputeRequest
from typer.testing import CliRunner

from finances_analytics.ft_indicators import api, cli, service
from finances_analytics.ft_indicators.request import load_request_file

from .conftest import DatasetFactory

BASE = "/indicators"
DS = "binance/spot/BTCUSDT/1h"
runner = CliRunner()

BODY = {
    "datasets": [
        {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}
    ],
    "indicators": [
        {"name": "rsi"},
        {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
        {"name": "bb", "params": {"std": 2.5}},
        {"name": "obv"},
    ],
}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


@pytest.fixture
def ds(hive_dataset: DatasetFactory) -> DatasetIdentifier:
    return hive_dataset(rows=400)


def _snapshot(settings: FinancesSettings) -> dict[str, bytes]:
    root = settings.INDICATORS_DIR
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.parquet"))}


def test_cli_and_api_print_same_json(client: TestClient, ds: DatasetIdentifier) -> None:
    task_id = client.post(f"{BASE}/compute", json=BODY).json()["task_id"]
    for command in ("plan", "status", "result"):
        out = runner.invoke(cli.app, [command, task_id])
        assert out.exit_code == 0, out.output
        assert json.loads(out.stdout) == client.get(f"{BASE}/{command}/{task_id}").json()
    show = runner.invoke(cli.app, ["show", DS])
    assert show.exit_code == 0, show.output
    assert json.loads(show.stdout) == client.get(f"{BASE}/datasets/{DS}").json()
    catalog = runner.invoke(cli.app, ["catalog"])
    assert catalog.exit_code == 0, catalog.output
    assert json.loads(catalog.stdout) == client.get(f"{BASE}/catalog").json()


def test_request_file_and_post_body_are_the_same_request(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(BODY), encoding="utf-8")
    assert load_request_file(path) == IndicatorComputeRequest.model_validate(BODY)


def test_request_file_and_post_body_produce_same_files(
    client: TestClient, ds: DatasetIdentifier, settings: FinancesSettings, tmp_path: Path
) -> None:
    api_task = client.post(f"{BASE}/compute", json=BODY).json()["task_id"]
    api_files = _snapshot(settings)
    api_plan = service.get_plan(api_task, settings)

    path = tmp_path / "request.json"
    path.write_text(json.dumps(BODY), encoding="utf-8")
    out = runner.invoke(cli.app, ["compute", "--request-file", str(path)])
    assert out.exit_code == 0, out.output
    assert _snapshot(settings) == api_files
    assert sorted(api_files) == sorted(
        f"binance/market_type=spot/symbol=BTCUSDT/timeframe=1h/year=2024/month=01/{sid}.parquet"
        for sid in ("rsi_14", "macd_12_26_9", "bb_20_2p5", "obv")
    )

    dry = runner.invoke(cli.app, ["compute", "--request-file", str(path), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert api_plan is not None
    assert json.loads(dry.stdout)["items"] == api_plan.model_dump(mode="json")["items"]
