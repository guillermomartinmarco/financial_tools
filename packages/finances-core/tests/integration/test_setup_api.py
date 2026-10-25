"""Tests de integración del router ``/setup`` (Art. 7 y Art. 18)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from ft_setup.api import router

UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_doctor_endpoint_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    async with _client() as client:
        response = await client.get("/setup/doctor")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["timestamp_ms"] > 0
    assert payload["summary"]["total"] == len(payload["vars"])


async def test_doctor_endpoint_masks_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FT_BINANCE_API_SECRET", "no-debe-salir")
    async with _client() as client:
        response = await client.get("/setup/doctor")
    assert "no-debe-salir" not in response.text
    secret = next(
        v for v in response.json()["vars"] if v["spec"]["name"] == "FT_BINANCE_API_SECRET"
    )
    assert secret["masked_value"] == "***"


async def test_init_dispatch_and_poll(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    async with _client() as client:
        created = await client.post("/setup/init")
        assert created.status_code == 202
        body = created.json()
        assert UUID7_RE.match(body["task_id"])
        assert body["status"] == "ACCEPTED"
        assert body["poll_url"].startswith("http://testserver/setup/status/")

        status_response = await client.get(f"/setup/status/{body['task_id']}")
        assert status_response.status_code == 200
        result = status_response.json()
        assert result["status"] == "COMPLETED"
        assert result["created_at_ms"] > 0
        assert (tmp_path / ".env.template").is_file()


async def test_init_dry_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    async with _client() as client:
        created = await client.post("/setup/init", params={"dry_run": True})
        task_id = created.json()["task_id"]
        status_response = await client.get(f"/setup/status/{task_id}")
    assert status_response.json()["status"] == "COMPLETED"
    assert not (tmp_path / ".env.template").exists()


async def test_status_unknown_task_returns_404(tmp_path: Path) -> None:
    async with _client() as client:
        response = await client.get("/setup/status/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


async def test_status_invalid_task_id_returns_422() -> None:
    async with _client() as client:
        response = await client.get("/setup/status/no-es-uuid")
    assert response.status_code == 422
