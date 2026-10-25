"""T7.5 — Contrato CLI vs API: mismos inputs, mismo DTO (Art. 1)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from finances_downloader.ft_market_data_binance import api, cli

from .conftest import ms, task_id_from

BASE = "/market-data/binance"


def test_cli_and_api_expose_same_status_result_and_plan(
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
) -> None:
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    runner = CliRunner()

    task_id = client.post(
        f"{BASE}/download",
        params={
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h"],
            "start_ms": ms(2024, 3, 1),
            "end_ms": ms(2024, 3, 2),
        },
    ).json()["task_id"]

    for command in ("status", "result", "plan"):
        cli_out = runner.invoke(cli.app, [command, task_id])
        assert cli_out.exit_code == 0, cli_out.output
        assert json.loads(cli_out.stdout) == client.get(f"{BASE}/{command}/{task_id}").json()


def test_cli_and_api_download_produce_same_data(
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
) -> None:
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    runner = CliRunner()

    api_task = client.post(
        f"{BASE}/download",
        params={
            "symbols": ["ETHUSDT"],
            "timeframes": ["4h"],
            "start_ms": ms(2024, 3, 1),
            "end_ms": ms(2024, 3, 3),
        },
    ).json()["task_id"]
    api_result = client.get(f"{BASE}/result/{api_task}").json()

    cli_out = runner.invoke(
        cli.app,
        ["download", "-s", "BTCUSDT", "-t", "4h", "--start", "2024-03-01", "--end", "2024-03-03"],
    )
    assert cli_out.exit_code == 0, cli_out.output
    cli_task = task_id_from(cli_out.stdout)
    cli_result = json.loads(runner.invoke(cli.app, ["result", cli_task]).stdout)

    assert api_result["stats"]["ETHUSDT/4h/spot"] == cli_result["stats"]["BTCUSDT/4h/spot"]
    assert api_result["status"] == cli_result["status"] == "COMPLETED"
