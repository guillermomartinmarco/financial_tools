"""T4.5: contrato CLI ≡ API (Art. 1): mismo JSON y misma petición canónica."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.ta_lib import TaLibComputeRequest
from typer.testing import CliRunner

from finances_analytics.ft_ta_lib import api, cli, service
from finances_analytics.ft_ta_lib.request import build_request, load_request_file

from .conftest import DatasetFactory

BASE = "/ta-lib"
DS = "binance/spot/BTCUSDT/1h"
runner = CliRunner()

BODY = {
    "datasets": [
        {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}
    ],
    "functions": [
        {"function": "rsi"},
        {"function": "macd", "params": {"fastperiod": 12, "slowperiod": 26, "signalperiod": 9}},
        {"function": "bbands", "params": {"nbdevup": 2.5, "matype": "ema"}},
        {"function": "cdlengulfing"},
    ],
    "groups": ["Math Operators"],
}
SIDS = (
    "talib_rsi_14",
    "talib_macd_12_26_9",
    "talib_bbands_20_2p5_2_ema",
    "talib_cdlengulfing",
)


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
    for args, params in (
        ([], {}),
        (["--group", "Pattern Recognition"], {"group": "Pattern Recognition"}),
        (["--function", "kama"], {"function": "kama"}),
    ):
        catalog = runner.invoke(cli.app, ["catalog", *args])
        assert catalog.exit_code == 0, catalog.output
        assert json.loads(catalog.stdout) == client.get(f"{BASE}/catalog", params=params).json()


def test_request_file_flags_and_post_body_are_the_same_request(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(BODY), encoding="utf-8")
    canonical = TaLibComputeRequest.model_validate(BODY)
    assert load_request_file(path) == canonical
    flags = build_request(
        [DS],
        [
            "rsi",
            "macd:fastperiod=12,slowperiod=26,signalperiod=9",
            "bbands:nbdevup=2.5,matype=ema",
            "CDLENGULFING",
        ],
        ["Math Operators"],
    )
    assert flags == canonical


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
    month = "binance/market_type=spot/symbol=BTCUSDT/timeframe=1h/year=2024/month=01"
    assert {f"{month}/{sid}.parquet" for sid in SIDS} <= set(api_files)

    dry = runner.invoke(cli.app, ["compute", "--request-file", str(path), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert api_plan is not None
    assert json.loads(dry.stdout)["items"] == api_plan.model_dump(mode="json")["items"]
