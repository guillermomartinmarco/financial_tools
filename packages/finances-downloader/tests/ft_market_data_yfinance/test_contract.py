"""T4.3 — Contrato CLI vs API: mismo JSON para ``status``/``plan``/``result`` (Art. 1)."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from finances_downloader.ft_market_data_yfinance import api, cli

from .conftest import FakeYahoo, all_days, business_days, task_id_from, yahoo_frame

BASE = "/market-data/yfinance"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_cli_and_api_expose_same_status_plan_and_result(use_fake_yahoo: FakeYahoo) -> None:
    use_fake_yahoo.add(
        "AAPL",
        "1d",
        yahoo_frame(business_days("AAPL", "2020-08-03", "2020-09-04"), dividends={4: 0.205}),
    )
    client = _client()
    runner = CliRunner()
    task_id = client.post(
        f"{BASE}/download",
        params={
            "symbols": ["AAPL", "NOPE123"],
            "timeframes": ["1d"],
            "start": "2020-08-01",
            "end": "2020-09-05",
        },
    ).json()["task_id"]

    for command in ("status", "plan", "result"):
        cli_out = runner.invoke(cli.app, [command, task_id])
        assert cli_out.exit_code == 0, cli_out.output
        assert json.loads(cli_out.stdout) == client.get(f"{BASE}/{command}/{task_id}").json()


def test_cli_and_api_download_produce_same_stats(use_fake_yahoo: FakeYahoo) -> None:
    use_fake_yahoo.add(
        "BTC-USD", "1d", yahoo_frame(all_days("BTC-USD", "2024-01-01", "2024-01-31"))
    )
    use_fake_yahoo.add(
        "ETH-USD", "1d", yahoo_frame(all_days("BTC-USD", "2024-01-01", "2024-01-31"))
    )
    client = _client()
    runner = CliRunner()
    api_task = client.post(
        f"{BASE}/download",
        params={
            "symbols": ["BTC-USD"],
            "timeframes": ["1d"],
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
    ).json()["task_id"]
    api_result = client.get(f"{BASE}/result/{api_task}").json()

    cli_out = runner.invoke(
        cli.app,
        ["download", "-s", "ETH-USD", "-t", "1d", "--start", "2024-01-01", "--end", "2024-02-01"],
    )
    assert cli_out.exit_code == 0, cli_out.output
    cli_result = json.loads(runner.invoke(cli.app, ["result", task_id_from(cli_out.stdout)]).stdout)
    assert api_result["stats"]["BTC-USD/1d"] == cli_result["stats"]["ETH-USD/1d"]
    assert api_result["status"] == cli_result["status"] == "COMPLETED"
