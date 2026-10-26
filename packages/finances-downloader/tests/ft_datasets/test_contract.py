"""T4.3 — Contrato CLI ≡ API: mismo JSON para ``show``, ``verify``, ``lock``, ``status``,
``plan`` y ``result`` (Art. 1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from finances_downloader.ft_datasets import api, cli

from .conftest import RECIPE_NAME, FakeRunner

BASE = "/datasets"


def _without(payload: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in fields}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake_runner = FakeRunner()
    monkeypatch.setattr(cli, "make_runner", lambda: fake_runner)
    return fake_runner


def test_cli_and_api_return_identical_json(recipe_dir: Path, fake: FakeRunner) -> None:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_runner] = lambda: fake
    client = TestClient(app)
    runner = CliRunner()

    def cli_json(*args: str) -> Any:
        result = runner.invoke(cli.app, ["--quiet", *args])
        return json.loads(result.stdout)

    task_id = client.post(f"{BASE}/materialize/{RECIPE_NAME}").json()["task_id"]
    for command in ("status", "plan", "result"):
        assert cli_json(command, task_id) == client.get(f"{BASE}/{command}/{task_id}").json()

    assert cli_json("show", RECIPE_NAME) == client.get(f"{BASE}/recipes/{RECIPE_NAME}").json()
    assert cli_json("list") == client.get(f"{BASE}/recipes").json()

    # lock: el mismo resultado (UNCHANGED la segunda vez) por ambas vías.
    assert cli_json("lock", RECIPE_NAME)["outcome"] == "WRITTEN"
    assert cli_json("lock", RECIPE_NAME) == client.post(f"{BASE}/lock/{RECIPE_NAME}").json()
    assert cli_json("show", RECIPE_NAME) == client.get(f"{BASE}/recipes/{RECIPE_NAME}").json()

    # verify: idéntico salvo la duración medida.
    cli_report = cli_json("verify", RECIPE_NAME)
    api_report = client.post(f"{BASE}/verify/{RECIPE_NAME}").json()
    assert cli_report["status"] == "OK"
    assert _without(cli_report, "duration_ms") == _without(api_report, "duration_ms")
    assert set(cli_report) == set(api_report)
