"""T5.1: router FastAPI de ft_market_data_manager (dispatch Art. 18) sobre un almacén temporal."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from finances_core.config import FinancesSettings
from finances_core.marketdata.tasks_lifecycle import create_download_task

from finances_downloader.ft_market_data_manager import api as api_module
from finances_downloader.ft_market_data_manager.api import router

from .conftest import (
    FEB_2024_MS,
    JAN_2024_MS,
    dataset_root,
    hours,
    make_ohlcv,
    write_partition_files,
)

BASE = "/market-data/manager"
UNKNOWN_ID = "0190b2a5-0000-7000-8000-000000000000"


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def store(parquet_dir: Path) -> Path:
    assert parquet_dir == FinancesSettings().PARQUET_DIR
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


def _files(parquet_dir: Path) -> list[str]:
    return sorted(str(p.relative_to(parquet_dir)) for p in parquet_dir.rglob("*.parquet"))


async def _wait(client: httpx.AsyncClient, poll_url: str) -> dict[str, Any]:
    for _ in range(200):
        body: dict[str, Any] = (await client.get(poll_url)).json()
        if body["status"] in {"COMPLETED", "FAILED"}:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("la tarea no terminó")


async def _run(client: httpx.AsyncClient, route: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST -> 202 -> polling hasta COMPLETED -> ``/result``."""
    res = await client.post(f"{BASE}/{route}", json=payload)
    assert res.status_code == 202, res.text
    accepted = res.json()
    assert accepted["poll_url"].endswith(f"{BASE}/status/{accepted['task_id']}")
    status = await _wait(client, accepted["poll_url"])
    assert status["status"] == "COMPLETED", status
    result = await client.get(f"{BASE}/result/{accepted['task_id']}")
    assert result.status_code == 200
    body: dict[str, Any] = result.json()
    return body


async def test_openapi_exposes_the_six_routes() -> None:
    async with _client() as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    assert {p: set(ops) for p, ops in paths.items()} == {
        f"{BASE}/compact": {"post"},
        f"{BASE}/dedupe": {"post"},
        f"{BASE}/prune": {"post"},
        f"{BASE}/import": {"post"},
        f"{BASE}/status/{{task_id}}": {"get"},
        f"{BASE}/result/{{task_id}}": {"get"},
    }


async def test_compact_returns_202_polls_and_returns_result(store: Path) -> None:
    async with _client() as client:
        res = await client.post(f"{BASE}/compact", json={"exchange": ["binance"]})
        assert res.status_code == 202
        accepted = res.json()
        assert set(accepted) == {"task_id", "poll_url"}
        status = await _wait(client, accepted["poll_url"])
        result = (await client.get(f"{BASE}/result/{accepted['task_id']}")).json()
    assert status["status"] == "COMPLETED"
    assert status["partitions_total"] == 2
    assert status["partitions_done"] == 2
    assert status["partitions_changed"] == 2
    assert status["partitions_conflict"] == 0
    assert status["rows_affected"] == 12
    assert status["bytes_after"] > 0
    assert status["errors_json"] == []
    assert result["action"] == "compact"
    assert [p["result"] for p in result["partitions"]] == ["changed", "changed"]
    root = dataset_root(store)
    assert len(list((root / "year=2024" / "month=01").glob("*.parquet"))) == 1


async def test_dry_run_goes_through_a_task_and_result_is_the_plan(store: Path) -> None:
    before = _files(store)
    async with _client() as client:
        body = await _run(client, "dedupe", {"dry_run": True})
    assert body["dry_run"] is True
    assert {p["result"] for p in body["partitions"]} == {"planned", "unchanged"}
    assert _files(store) == before


async def test_dedupe_removes_duplicates(store: Path) -> None:
    async with _client() as client:
        body = await _run(client, "dedupe", {"start_ms": FEB_2024_MS})
    assert [(p["month"], p["rows_before"], p["rows_after"]) for p in body["partitions"]] == [
        (2, 6, 4)
    ]


async def test_prune_with_confirm_deletes_complete_months(store: Path) -> None:
    async with _client() as client:
        body = await _run(client, "prune", {"before_ms": FEB_2024_MS, "confirm": True})
    assert [p["month"] for p in body["partitions"]] == [1]
    assert all("month=02" in f for f in _files(store))


async def test_prune_without_confirm_nor_dry_run_is_400_and_deletes_nothing(store: Path) -> None:
    before = _files(store)
    async with _client() as client:
        res = await client.post(f"{BASE}/prune", json={"before_ms": FEB_2024_MS})
    assert res.status_code == 400
    assert _files(store) == before


async def test_prune_dry_run_needs_no_confirmation(store: Path) -> None:
    before = _files(store)
    async with _client() as client:
        body = await _run(client, "prune", {"all": True, "exchange": ["binance"], "dry_run": True})
    assert {p["result"] for p in body["partitions"]} == {"planned"}
    assert _files(store) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"confirm": True},
        {"all": True, "before_ms": 5, "confirm": True},
        {"all": True, "confirm": True},
    ],
)
async def test_prune_invalid_criterion_is_422(store: Path, payload: dict[str, Any]) -> None:
    before = _files(store)
    async with _client() as client:
        res = await client.post(f"{BASE}/prune", json=payload)
    assert res.status_code == 422
    assert _files(store) == before


@pytest.mark.parametrize("route", ["compact", "dedupe"])
async def test_inverted_range_is_400(store: Path, route: str) -> None:
    async with _client() as client:
        res = await client.post(f"{BASE}/{route}", json={"start_ms": 10, "end_ms": 5})
    assert res.status_code == 400


async def test_invalid_body_is_422(store: Path) -> None:
    async with _client() as client:
        res = await client.post(f"{BASE}/compact", json={"start_ms": "abc"})
    assert res.status_code == 422


async def test_result_of_unfinished_task_is_409(store: Path) -> None:
    create_download_task(UNKNOWN_ID, {"action": "compact"})
    async with _client() as client:
        res = await client.get(f"{BASE}/result/{UNKNOWN_ID}")
        status = await client.get(f"{BASE}/status/{UNKNOWN_ID}")
    assert res.status_code == 409
    assert status.json()["status"] == "PENDING"
    assert status.json()["partitions_total"] == 0


async def test_unknown_task_is_404(store: Path) -> None:
    async with _client() as client:
        assert (await client.get(f"{BASE}/status/{UNKNOWN_ID}")).status_code == 404
        assert (await client.get(f"{BASE}/result/{UNKNOWN_ID}")).status_code == 404
        assert (await client.get(f"{BASE}/status/not-a-uuid")).status_code == 422


async def test_partition_in_error_does_not_fail_the_task(store: Path) -> None:
    broken = dataset_root(store) / "year=2024" / "month=03"
    broken.mkdir(parents=True)
    (broken / "data.parquet").write_bytes(b"not parquet")
    async with _client() as client:
        res = await client.post(f"{BASE}/compact", json={})
        accepted = res.json()
        status = await _wait(client, accepted["poll_url"])
        result = (await client.get(f"{BASE}/result/{accepted['task_id']}")).json()
    assert status["status"] == "COMPLETED"
    assert status["partitions_changed"] == 2
    assert len(status["errors_json"]) == 1
    assert "2024-03" in status["errors_json"][0]
    assert {p["month"]: p["result"] for p in result["partitions"]}[3] == "error"


async def test_partition_in_conflict_does_not_fail_the_task(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module.service.planner, "commit", lambda *_a, **_k: "conflict")
    async with _client() as client:
        res = await client.post(f"{BASE}/compact", json={})
        status = await _wait(client, res.json()["poll_url"])
    assert status["status"] == "COMPLETED"
    assert status["partitions_conflict"] == 2
    assert status["partitions_changed"] == 0


async def test_unexpected_exception_marks_task_failed(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(api_module.service, "run_compact", boom)
    async with _client() as client:
        res = await client.post(f"{BASE}/compact", json={})
        status = await _wait(client, res.json()["poll_url"])
        result = await client.get(f"{BASE}/result/{res.json()['task_id']}")
    assert status["status"] == "FAILED"
    assert "disco lleno" in json.dumps(status["errors_json"])
    assert result.status_code == 500
