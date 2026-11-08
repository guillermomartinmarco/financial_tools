"""T7.2 (parcial): contrato CLI <-> API de compact/dedupe/prune (la importación llega después).

La misma secuencia se ejecuta por la CLI (salida JSON) sobre un almacén temporal y por la
API (POST -> polling hasta ``COMPLETED`` -> ``/result``) sobre otro almacén temporal
independiente: los ``ManagerResult`` y el contenido final de los almacenes deben coincidir.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import structlog
from fastapi import FastAPI
from finances_core.config import FinancesSettings
from typer.testing import CliRunner

from finances_downloader.ft_market_data_manager.api import router
from finances_downloader.ft_market_data_manager.cli import app

from .conftest import FEB_2024_MS, JAN_2024_MS, hours, make_ohlcv, write_partition_files

BASE = "/market-data/manager"
SEQUENCE: list[tuple[str, dict[str, Any]]] = [
    ("compact", {"dry_run": True}),
    ("dedupe", {"dry_run": True}),
    ("compact", {"exchange": ["binance"], "symbol": ["BTCUSDT"]}),
    ("dedupe", {"timeframe": ["1h"], "start_ms": FEB_2024_MS}),
    ("prune", {"all": True, "exchange": ["binance"], "dry_run": True}),
    ("prune", {"before_ms": FEB_2024_MS, "confirm": True}),
    ("prune", {"before_ms": FEB_2024_MS, "confirm": True}),
]


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    logging.basicConfig(force=True, handlers=[logging.NullHandler()])
    structlog.reset_defaults()


def _seed(root: Path) -> Path:
    """Almacén de partida idéntico en cada raíz; devuelve su ``PARQUET_DIR``."""
    parquet_dir = FinancesSettings().PARQUET_DIR
    assert parquet_dir.is_relative_to(root), "el almacén debe ser el temporal"
    jan = hours(JAN_2024_MS, 6)
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [make_ohlcv(jan[3:]), make_ohlcv(jan[:3])],
        names=["a.parquet", "b.parquet"],
    )
    feb = hours(FEB_2024_MS, 4)
    write_partition_files(
        parquet_dir, 2024, 2, [make_ohlcv(feb), make_ohlcv(feb[:2], close_offset=5.0)]
    )
    return parquet_dir


def _cli_args(action: str, payload: dict[str, Any]) -> list[str]:
    args = [action, "-f", "json"]
    for key, flag in (
        ("exchange", "--exchange"),
        ("market_type", "--market-type"),
        ("symbol", "--symbol"),
        ("timeframe", "--timeframe"),
    ):
        for value in payload.get(key, []):
            args += [flag, value]
    for key, flag in (("start_ms", "--start"), ("before_ms", "--before")):
        if key in payload:
            args += [flag, str(payload[key])]
    if payload.get("dry_run"):
        args.append("--dry-run")
    if payload.get("all"):
        args.append("--all")
    if payload.get("confirm"):
        args.append("--force")
    return args


def _snapshot(parquet_dir: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Contenido lógico del almacén: ruta relativa -> filas (independiente de mtime/bytes)."""
    return {
        str(p.relative_to(parquet_dir)): pl.read_parquet(p).rows()
        for p in sorted(parquet_dir.rglob("*.parquet"))
    }


def _normalize(result: dict[str, Any]) -> dict[str, Any]:
    """Nada que normalizar en el resultado (sin ids ni timestamps); se deja explícito."""
    return json.loads(json.dumps(result, sort_keys=True))


def _run_cli(sequence: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action, payload in sequence:
        res = CliRunner().invoke(app, _cli_args(action, payload))
        assert res.exit_code == 0, res.output
        out.append(_normalize(json.loads(res.stdout)))
    return out


async def _run_api(sequence: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    api = FastAPI()
    api.include_router(router)
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t") as c:
        for action, payload in sequence:
            res = await c.post(f"{BASE}/{action}", json=payload)
            assert res.status_code == 202, res.text
            task = res.json()
            for _ in range(200):
                status = (await c.get(task["poll_url"])).json()
                if status["status"] in {"COMPLETED", "FAILED"}:
                    break
                await asyncio.sleep(0.05)
            assert status["status"] == "COMPLETED", status
            result = await c.get(f"{BASE}/result/{task['task_id']}")
            out.append(_normalize(result.json()))
    return out


async def test_same_sequence_gives_equivalent_results_and_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_cli, root_api = tmp_path / "cli", tmp_path / "api"
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(root_cli))
        store_cli = _seed(root_cli)
        cli_results = _run_cli(SEQUENCE)
        cli_store = _snapshot(store_cli)
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(root_api))
        store_api = _seed(root_api)
        api_results = await _run_api(SEQUENCE)
        api_store = _snapshot(store_api)
    assert store_cli != store_api
    assert cli_results == api_results
    assert cli_store == api_store
    # La secuencia hizo algo observable: compactó, deduplicó y borró enero.
    assert [r["result"] for r in cli_results[2]["partitions"]] == ["changed", "changed"]
    assert cli_results[5]["partitions"][0]["month"] == 1
    assert cli_results[6]["partitions"] == []


def _cli_exit(args: list[str], input_: str | None = None) -> int:
    return CliRunner().invoke(app, args, input=input_).exit_code


async def _api_status(client: httpx.AsyncClient, route: str, payload: dict[str, Any]) -> int:
    return (await client.post(f"{BASE}/{route}", json=payload)).status_code


CASES: list[tuple[str, list[str], str | None, dict[str, Any], tuple[int, ...]]] = [
    # (ruta, args CLI, entrada CLI, payload API, estados HTTP aceptables)
    ("prune", ["prune", "--force"], None, {"confirm": True}, (400, 422)),
    ("prune", ["prune", "--all", "--force"], None, {"all": True, "confirm": True}, (400, 422)),
    (
        "prune",
        ["prune", "--all", "--before", "5", "--force"],
        None,
        {"all": True, "before_ms": 5, "confirm": True},
        (400, 422),
    ),
    (
        "compact",
        ["compact", "--start", "9", "--end", "1"],
        None,
        {"start_ms": 9, "end_ms": 1},
        (400, 422),
    ),
    (
        "dedupe",
        ["dedupe", "--start", "9", "--end", "1"],
        None,
        {"start_ms": 9, "end_ms": 1},
        (400, 422),
    ),
    (
        "compact",
        ["compact", "--start", "not-a-date"],
        None,
        {"start_ms": 9, "end_ms": 1},
        (400, 422),
    ),
]


@pytest.mark.parametrize(("route", "args", "input_", "payload", "http"), CASES)
async def test_invalid_arguments_are_equivalent_and_delete_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    args: list[str],
    input_: str | None,
    payload: dict[str, Any],
    http: tuple[int, ...],
) -> None:
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(tmp_path))
        store = _seed(tmp_path)
        before = _snapshot(store)
        api = FastAPI()
        api.include_router(router)
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            status = await _api_status(client, route, payload)
        assert _cli_exit(args, input_) == 2
        assert status in http
        assert _snapshot(store) == before


async def test_missing_confirmation_is_refused_by_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin ``--force`` el rechazo interactivo da exit 1; la API sin ``confirm`` da 400."""
    with monkeypatch.context() as m:
        m.setenv("FT_PROJECT_ROOT", str(tmp_path))
        store = _seed(tmp_path)
        before = _snapshot(store)
        api = FastAPI()
        api.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://t"
        ) as client:
            status = await _api_status(client, "prune", {"before_ms": FEB_2024_MS})
        refused = _cli_exit(["prune", "--before", str(FEB_2024_MS)], "n\n")
        assert (refused, status) == (1, 400)
        assert _snapshot(store) == before
