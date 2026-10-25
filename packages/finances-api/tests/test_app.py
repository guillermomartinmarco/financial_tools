"""Tests de la raíz de composición HTTP (specs/finances_api)."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from finances_api import __version__, cli
from finances_api.app import create_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FT_PROJECT_ROOT", str(tmp_path))


async def test_health_returns_ok_and_version() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "parquet_dir_accessible": False,
    }


async def test_health_reports_accessible_parquet_dir(tmp_path: Path) -> None:
    (tmp_path / "data" / "parquet").mkdir(parents=True)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.json()["parquet_dir_accessible"] is True


async def test_openapi_includes_all_tool_routers() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    prefixes = ("/setup/", "/market-data/binance/", "/market-data/yfinance/", "/data/")
    for prefix in prefixes:
        assert any(path.startswith(prefix) for path in paths), prefix


async def test_openapi_includes_the_four_yfinance_routes() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    yf = {
        path: set(ops) for path, ops in paths.items() if path.startswith("/market-data/yfinance/")
    }
    assert yf == {
        "/market-data/yfinance/download": {"post"},
        "/market-data/yfinance/status/{task_id}": {"get"},
        "/market-data/yfinance/plan/{task_id}": {"get"},
        "/market-data/yfinance/result/{task_id}": {"get"},
    }


async def test_mounted_setup_router_answers() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/setup/doctor")
    assert response.status_code == 200
    assert "vars" in response.json()


async def test_lifespan_configures_json_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "finances_api.app.configure_logging",
        lambda mode, level, **kwargs: calls.append({"mode": mode, **kwargs}),
    )
    app = create_app()
    async with app.router.lifespan_context(app):
        pass
    assert calls == [{"mode": "api", "component": "finances_api"}]


def test_ft_serve_help() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for option in ("--host", "--port", "--log-level"):
        assert option in result.output


def test_ft_serve_without_uvicorn_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "uvicorn":
            raise ImportError("no uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    result = runner.invoke(cli.app, [])
    assert result.exit_code == cli.EXIT_MISSING_DEPENDENCY
    assert "finances-api[service]" in result.output


def test_ft_serve_runs_uvicorn_with_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeUvicorn:
        @staticmethod
        def run(app: object, **kwargs: Any) -> None:
            captured.update(kwargs, app=app)

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setenv("FT_API_PORT", "8123")
    result = runner.invoke(cli.app, ["--host", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123
    assert json.dumps(captured["log_config"]) == "null"
