"""T7.2 (import): contrato CLI <-> API de ``import`` sobre dos almacenes temporales independientes.

La misma secuencia (dry-run y confirmada) se ejecuta por la CLI (salida JSON) en un almacén y por
la API (POST -> polling -> ``/result``) en otro: los ``ManagerResult`` (con su ``import_plan``) y
el contenido final de los almacenes deben coincidir. Los orígenes viven fuera de ambos almacenes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from finances_core.config import FinancesSettings
from typer.testing import CliRunner

from finances_downloader.ft_market_data_manager.api import router
from finances_downloader.ft_market_data_manager.cli import app

from .conftest import JAN_2024_MS, hours, make_ohlcv
from .import_helpers import make_binance, write_source
from .test_contract import BASE, _normalize, _seed, _snapshot

DEST = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}
ETH = {**DEST, "symbol": "ETHUSDT"}
HOUR = 3_600_000


def _sources(directory: Path) -> dict[str, Path]:
    jan = JAN_2024_MS
    return {
        "new": write_source(directory, make_ohlcv(hours(jan + 10 * HOUR, 3)), "csv", "new"),
        "mixed": write_source(directory, make_ohlcv(hours(jan + 4 * HOUR, 4)), "jsonl", "mixed"),
        "fix": write_source(
            directory, make_ohlcv(hours(jan, 2), close_offset=0.4), "parquet", "fix"
        ),
        "eth": write_source(directory, make_binance(hours(jan, 3)), "parquet", "eth"),
        "invalid": write_source(directory, make_ohlcv(hours(jan, 2)).drop("close"), "csv", "inv"),
    }


def _sequence(src: dict[str, Path]) -> list[tuple[dict[str, Any], bool]]:
    """``(payload API, confirmada)``; los payload usan las mismas claves que ``ImportRequest``."""
    return [
        ({**DEST, "source": src["new"], "dry_run": True}, False),
        ({**DEST, "source": src["new"], "confirm": True}, True),
        ({**DEST, "source": src["new"], "confirm": True}, True),  # idempotente
        ({**DEST, "source": src["mixed"], "dry_run": True}, False),
        ({**DEST, "source": src["mixed"], "confirm": True, "on_conflict": "replace"}, True),
        ({**DEST, "source": src["fix"], "confirm": True, "on_conflict": "replace"}, True),
        ({**ETH, "source": src["eth"], "dry_run": True, "create": True}, False),
        ({**ETH, "source": src["eth"], "confirm": True, "create": True}, True),
    ]


def _cli_args(payload: dict[str, Any]) -> list[str]:
    args = ["-f", "json", "import", "--source", str(payload["source"])]
    for key, flag in (
        ("exchange", "-e"),
        ("market_type", "-m"),
        ("symbol", "-s"),
        ("timeframe", "-t"),
        ("on_conflict", "--on-conflict"),
    ):
        if key in payload:
            args += [flag, payload[key]]
    for key, flag in (("create", "--create"), ("dry_run", "--dry-run"), ("force", "--force")):
        if payload.get(key):
            args.append(flag)
    return args


def _run_cli(sequence: list[tuple[dict[str, Any], bool]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for payload, confirmed in sequence:
        res = CliRunner().invoke(app, _cli_args(payload), input="y\n" if confirmed else None)
        assert res.exit_code == 0, res.output
        out.append(_normalize(json.loads(res.stdout[res.stdout.index("{") :])))
    return out


async def _post(client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    body = {**payload, "source": str(payload["source"])}
    return await client.post(f"{BASE}/import", json=body)


async def _wait(client: httpx.AsyncClient, poll_url: str) -> dict[str, Any]:
    for _ in range(200):
        status: dict[str, Any] = (await client.get(poll_url)).json()
        if status["status"] in {"COMPLETED", "FAILED"}:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError("la tarea no terminó")


async def _run_api(sequence: list[tuple[dict[str, Any], bool]]) -> list[dict[str, Any]]:
    api = FastAPI()
    api.include_router(router)
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t") as c:
        for payload, _ in sequence:
            res = await _post(c, payload)
            assert res.status_code == 202, res.text
            task = res.json()
            assert (await _wait(c, task["poll_url"]))["status"] == "COMPLETED"
            out.append(_normalize((await c.get(f"{BASE}/result/{task['task_id']}")).json()))
    return out


async def test_same_import_sequence_gives_equivalent_results_and_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = _sequence(_sources(tmp_path / "incoming"))
    root_cli, root_api = tmp_path / "cli", tmp_path / "api"
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(root_cli))
        store_cli = _seed(root_cli)
        assert store_cli == FinancesSettings().PARQUET_DIR
        cli_results = _run_cli(sequence)
        cli_store = _snapshot(store_cli)
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(root_api))
        store_api = _seed(root_api)
        assert store_api == FinancesSettings().PARQUET_DIR
        api_results = await _run_api(sequence)
        api_store = _snapshot(store_api)
    assert store_cli != store_api
    assert cli_results == api_results
    assert cli_store == api_store
    # La secuencia hizo algo observable.
    assert cli_results[0]["import_plan"]["rows_new"] == 3
    assert [r["result"] for r in cli_results[1]["partitions"]] == ["changed"]
    assert [r["result"] for r in cli_results[2]["partitions"]] == ["unchanged"]
    assert cli_results[3]["import_plan"]["rows_conflict"] == 2
    assert cli_results[5]["import_plan"]["max_close_divergence"] is not None
    assert any("ETHUSDT" in key for key in cli_store)


ERROR_CASES = [
    # (clave de origen, extra, exit CLI, estado HTTP del POST, estado HTTP de /result o None)
    ("invalid", {}, 2, 202, 422),
    ("mixed", {"on_conflict": "fail"}, 3, 202, 409),
    ("missing", {}, 1, 404, None),
    ("new", {"symbol": "XRPUSDT"}, 1, 404, None),
]


@pytest.mark.parametrize(("key", "extra", "exit_code", "post_status", "result_status"), ERROR_CASES)
async def test_error_mapping_is_equivalent_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    extra: dict[str, Any],
    exit_code: int,
    post_status: int,
    result_status: int | None,
) -> None:
    sources = _sources(tmp_path / "incoming")
    sources["missing"] = tmp_path / "incoming" / "no-existe.csv"
    payload = {**DEST, "source": sources[key], "confirm": True, **extra}
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(tmp_path / "cli"))
        store_cli = _seed(tmp_path / "cli")
        before = _snapshot(store_cli)
        res = CliRunner().invoke(app, _cli_args(payload), input="y\n")
        assert res.exit_code == exit_code, res.output
        assert _snapshot(store_cli) == before
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(tmp_path / "api"))
        store_api = _seed(tmp_path / "api")
        api = FastAPI()
        api.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://t"
        ) as c:
            posted = await _post(c, payload)
            assert posted.status_code == post_status, posted.text
            if result_status is not None:
                task = posted.json()
                assert (await _wait(c, task["poll_url"]))["status"] == "FAILED"
                result = await c.get(f"{BASE}/result/{task['task_id']}")
                assert result.status_code == result_status
        assert _snapshot(store_api) == before
