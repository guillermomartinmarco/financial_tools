"""Contrato de paridad CLI vs API de ``ft_strategy_manager`` (Art. 1, T6.6).

Ejecuta la misma secuencia de operaciones contra dos bases independientes, una por el CLI
(``--format json``) y otra por la API, y exige respuestas idénticas una vez normalizados los
valores generados (UUID v7 y timestamps).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
import yaml
from fastapi import FastAPI
from typer.testing import CliRunner

from ft_strategy_manager import cli
from ft_strategy_manager.api import router

runner = CliRunner()

PARAMS_V1 = {"window": 10, "mode": "a", "nested": {"k": [1, 2.5, True, None]}}
PARAMS_V2 = {"window": 20, "mode": "a", "nested": {"k": [1, 2.5, True, None]}, "new": "x"}


@pytest.fixture(autouse=True)
def _default_structlog() -> None:
    structlog.reset_defaults()


def _normalize(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            k: (0 if k.endswith("_at_ms") and v is not None else _normalize(v, ids))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_normalize(v, ids) for v in value]
    if isinstance(value, str) and value in ids:
        return ids[value]
    return value


def _cli_json(*args: str) -> Any:
    result = runner.invoke(cli.app, ["--quiet", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _api_client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _scenario_cli(tmp_path: Path) -> dict[str, Any]:
    params1 = tmp_path / "p1.json"
    params2 = tmp_path / "p2.json"
    params1.write_text(json.dumps(PARAMS_V1), encoding="utf-8")
    params2.write_text(json.dumps(PARAMS_V2), encoding="utf-8")
    out: dict[str, Any] = {}
    out["create"] = _cli_json(
        "create",
        "alpha",
        "-p",
        str(params1),
        "-d",
        "desc",
        "-t",
        "fx",
        "--user",
        "ana",
        "-f",
        "json",
    )
    sid = out["create"]["strategy_id"]
    out["update"] = _cli_json("update", sid, "-p", str(params2), "--user", "bo", "-f", "json")
    out["get_latest"] = _cli_json("get", sid, "-f", "json")
    out["get_v1"] = _cli_json("get", sid, "--version", "1", "-f", "json")
    out["list"] = _cli_json("list", "--tag", "fx", "-f", "json")
    out["versions"] = _cli_json("versions", sid, "-f", "json")
    out["diff"] = _cli_json("diff", sid, "--from", "1", "--to", "2", "-f", "json")
    exported = runner.invoke(cli.app, ["--quiet", "export", sid, "--format", "yaml"])
    out["export_yaml"] = yaml.safe_load(exported.stdout)
    _cli_run_ok("delete", sid)
    out["list_after_delete"] = _cli_json("list", "-f", "json")
    out["list_with_deleted"] = _cli_json("list", "--include-deleted", "-f", "json")
    out["id"] = sid
    return out


def _cli_run_ok(*args: str) -> None:
    result = runner.invoke(cli.app, ["--quiet", *args])
    assert result.exit_code == 0, result.output


async def _scenario_api() -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with _api_client() as client:
        created = await client.post(
            "/strategies",
            json={
                "name": "alpha",
                "description": "desc",
                "parameters": PARAMS_V1,
                "tags": ["fx"],
            },
            headers={"X-User-ID": "ana"},
        )
        out["create"] = created.json()
        sid = out["create"]["strategy_id"]
        out["update"] = (
            await client.put(
                f"/strategies/{sid}", json={"parameters": PARAMS_V2}, headers={"X-User-ID": "bo"}
            )
        ).json()
        out["get_latest"] = (await client.get(f"/strategies/{sid}")).json()
        out["get_v1"] = (await client.get(f"/strategies/{sid}", params={"version": 1})).json()
        out["list"] = (await client.get("/strategies", params={"tag": "fx"})).json()
        out["versions"] = (await client.get(f"/strategies/{sid}/versions")).json()
        out["diff"] = (
            await client.get(f"/strategies/{sid}/diff", params={"from": 1, "to": 2})
        ).json()
        out["export_yaml"] = yaml.safe_load(
            (await client.get(f"/strategies/{sid}/export", params={"format": "yaml"})).content
        )
        await client.delete(f"/strategies/{sid}")
        out["list_after_delete"] = (await client.get("/strategies")).json()
        out["list_with_deleted"] = (
            await client.get("/strategies", params={"include_deleted": True})
        ).json()
        out["id"] = sid
    return out


async def test_cli_and_api_produce_identical_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FT_STRATEGIES_DB_PATH", str(tmp_path / "cli.db"))
    via_cli = await _scenario_cli(tmp_path)
    monkeypatch.setenv("FT_STRATEGIES_DB_PATH", str(tmp_path / "api.db"))
    via_api = await _scenario_api()

    ids_cli = {via_cli["id"]: "<id>"}
    ids_api = {via_api["id"]: "<id>"}
    # La clave de export lleva un timestamp propio.
    for scenario in (via_cli, via_api):
        scenario["export_yaml"].pop("exported_at_ms")
    for key in via_cli:
        if key == "id":
            continue
        assert _normalize(via_cli[key], ids_cli) == _normalize(via_api[key], ids_api), key


@pytest.mark.parametrize(
    ("label", "cli_args", "api_call", "api_status"),
    [
        ("get missing", ["get", "{ghost}"], lambda c, g: c.get(f"/strategies/{g}"), 404),
        (
            "diff missing",
            ["diff", "{ghost}", "--from", "1", "--to", "2"],
            lambda c, g: c.get(f"/strategies/{g}/diff", params={"from": 1, "to": 2}),
            404,
        ),
        ("delete missing", ["delete", "{ghost}"], lambda c, g: c.delete(f"/strategies/{g}"), 204),
    ],
)
async def test_cli_and_api_agree_on_error_semantics(
    label: str,
    cli_args: list[str],
    api_call: Callable[[httpx.AsyncClient, str], Any],
    api_status: int,
) -> None:
    ghost = "00000000-0000-7000-8000-000000000000"
    result = runner.invoke(cli.app, ["--quiet", *[a.replace("{ghost}", ghost) for a in cli_args]])
    async with _api_client() as client:
        response = await api_call(client, ghost)
    assert response.status_code == api_status, label
    # 404 (API) <-> exit 1 (CLI); 204 idempotente (API) <-> exit 0 (CLI).
    expected_exit = {404: cli.EXIT_NOT_FOUND, 204: cli.EXIT_OK}[api_status]
    assert result.exit_code == expected_exit, label
    if api_status == 404:
        assert response.json()["detail"] in result.stderr


async def test_cli_and_api_agree_on_validation_conflict_and_no_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FT_STRATEGIES_DB_PATH", str(tmp_path / "shared.db"))
    params = tmp_path / "p.json"
    params.write_text(json.dumps(PARAMS_V1), encoding="utf-8")
    sid = _cli_json("create", "alpha", "-p", str(params), "-f", "json")["strategy_id"]
    async with _api_client() as client:
        conflict = await client.post("/strategies", json={"name": "alpha", "parameters": {}})
        no_change = await client.put(f"/strategies/{sid}", json={"parameters": PARAMS_V1})
        invalid = await client.post("/strategies", json={"name": "Bad Name"})
    cli_conflict = runner.invoke(cli.app, ["--quiet", "create", "alpha", "-p", str(params)])
    cli_no_change = runner.invoke(cli.app, ["--quiet", "update", sid, "-p", str(params)])
    cli_invalid = runner.invoke(cli.app, ["--quiet", "create", "Bad Name", "-p", str(params)])
    assert (conflict.status_code, cli_conflict.exit_code) == (409, cli.EXIT_CONFLICT)
    assert (no_change.status_code, cli_no_change.exit_code) == (400, cli.EXIT_VALIDATION)
    assert (invalid.status_code, cli_invalid.exit_code) == (422, cli.EXIT_VALIDATION)
    assert no_change.json()["detail"] in cli_no_change.stderr
