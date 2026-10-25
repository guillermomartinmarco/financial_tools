"""Tests de la raíz de composición HTTP (specs/finances_api)."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.market_data import OHLCV_SCHEMA
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
    prefixes = (
        "/setup/",
        "/market-data/binance/",
        "/market-data/yfinance/",
        "/data/",
        "/indicators/",
    )
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


async def test_openapi_includes_the_seven_indicators_routes() -> None:
    """``ft_indicators`` (plan §2.2) queda montado con sus 7 rutas bajo ``/indicators``."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    indicators = {path: set(ops) for path, ops in paths.items() if path.startswith("/indicators/")}
    dataset = "/indicators/datasets/{exchange}/{market_type}/{symbol}/{timeframe}"
    assert indicators == {
        "/indicators/catalog": {"get"},
        "/indicators/compute": {"post"},
        "/indicators/status/{task_id}": {"get"},
        "/indicators/plan/{task_id}": {"get"},
        "/indicators/result/{task_id}": {"get"},
        dataset: {"get"},
        f"{dataset}/export": {"post"},
    }


async def test_mounted_indicators_router_answers() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/indicators/catalog")
    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()]
    assert len(names) == 12
    assert {"rsi", "macd", "vwap"} <= set(names)


def _write_binance_dataset(rows: int = 60) -> None:
    """Dataset Hive canónico mínimo ``binance/spot/BTCUSDT/1h`` bajo ``FT_PARQUET_DIR``."""
    start = 1_704_067_200_000
    prices = [100.0 + i for i in range(rows)]
    frame = pl.DataFrame(
        {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "market_type": "spot",
            "open_time_ms": [start + i * 3_600_000 for i in range(rows)],
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [10.0] * rows,
            "quote_volume": [1000.0] * rows,
            "trades_count": [5] * rows,
            "taker_buy_base_volume": [5.0] * rows,
            "taker_buy_quote_volume": [500.0] * rows,
        }
    )
    dt = pl.from_epoch(pl.col("open_time_ms"), time_unit="ms")
    frame = frame.select([pl.col(c).cast(t) for c, t in OHLCV_SCHEMA.items()]).with_columns(
        dt.dt.year().alias("year"), dt.dt.month().alias("month")
    )
    write_parquet_partitioned(
        frame, FinancesSettings().PARQUET_DIR, exchange="binance", schema=dict(OHLCV_SCHEMA)
    )


async def test_indicators_dispatch_urls_point_to_indicators_routes() -> None:
    """En la app compuesta, ``poll_url``/``plan_url``/``result_url`` de ``POST /indicators/compute``
    apuntan a ``/indicators`` (los nombres de ruta no colisionan con los de otros routers)."""
    _write_binance_dataset()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/indicators/compute",
            json={
                "datasets": [
                    {
                        "exchange": "binance",
                        "market_type": "spot",
                        "symbol": "BTCUSDT",
                        "timeframe": "1h",
                    }
                ],
                "indicators": [{"name": "rsi"}],
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        task_id = body["task_id"]
        assert body["poll_url"] == f"http://test/indicators/status/{task_id}"
        assert body["plan_url"] == f"http://test/indicators/plan/{task_id}"
        assert body["result_url"] == f"http://test/indicators/result/{task_id}"
        polled = await client.get(body["poll_url"])
    assert polled.status_code == 200
    assert polled.json()["status"] == "COMPLETED"


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
