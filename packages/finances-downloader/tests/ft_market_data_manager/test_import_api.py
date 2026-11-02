"""T6.3: ``POST /market-data/manager/import`` (Art. 18) sobre un almacén temporal."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
from fastapi import FastAPI
from finances_core.config import FinancesSettings

from finances_downloader.ft_market_data_manager.api import router

from .conftest import FEB_2024_MS, JAN_2024_MS, hours, make_ohlcv, write_partition_files
from .import_helpers import make_binance, read_dataset, tree_fingerprint, write_source

BASE = "/market-data/manager"
DEST = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}


@pytest.fixture(autouse=True)
def _tmp_store_only(parquet_dir: Path, tmp_path: Path) -> None:
    assert parquet_dir == FinancesSettings().PARQUET_DIR
    assert parquet_dir.is_relative_to(tmp_path)


@pytest.fixture
def store(parquet_dir: Path) -> Path:
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 6))])
    write_partition_files(parquet_dir, 2024, 2, [make_ohlcv(hours(FEB_2024_MS, 4))])
    return parquet_dir


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _payload(source: Path, **extra: Any) -> dict[str, Any]:
    return {"source": str(source), **DEST, **extra}


async def _wait(client: httpx.AsyncClient, poll_url: str) -> dict[str, Any]:
    for _ in range(200):
        body: dict[str, Any] = (await client.get(poll_url)).json()
        if body["status"] in {"COMPLETED", "FAILED"}:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("la tarea no terminó")


def _new_source(tmp_path: Path, n: int = 2) -> Path:
    return write_source(
        tmp_path / "incoming", make_ohlcv(hours(JAN_2024_MS + 10 * 3_600_000, n)), "csv"
    )


async def test_import_returns_202_polls_and_returns_result(store: Path, tmp_path: Path) -> None:
    src = _new_source(tmp_path)
    async with _client() as client:
        res = await client.post(f"{BASE}/import", json=_payload(src, confirm=True))
        assert res.status_code == 202, res.text
        accepted = res.json()
        assert set(accepted) == {"task_id", "poll_url"}
        status = await _wait(client, accepted["poll_url"])
        assert status["status"] == "COMPLETED"
        assert (status["partitions_total"], status["partitions_done"]) == (1, 1)
        assert status["partitions_changed"] == 1
        result = (await client.get(f"{BASE}/result/{accepted['task_id']}")).json()
    assert result["action"] == "import"
    assert result["dry_run"] is False
    assert result["import_plan"]["rows_new"] == 2
    assert result["import_plan"]["partitions_modified"] == ["2024-01"]
    assert read_dataset(store.joinpath("binance")).height == 12


async def test_import_without_confirm_nor_dry_run_is_400(store: Path, tmp_path: Path) -> None:
    src = _new_source(tmp_path)
    before = tree_fingerprint(store)
    async with _client() as client:
        res = await client.post(f"{BASE}/import", json=_payload(src))
        forced = await client.post(f"{BASE}/import", json=_payload(src, force=True))
    assert res.status_code == 400
    assert forced.status_code == 400
    assert tree_fingerprint(store) == before


async def test_import_dry_run_returns_plan_and_writes_nothing(store: Path, tmp_path: Path) -> None:
    src = _new_source(tmp_path)
    before = tree_fingerprint(store)
    async with _client() as client:
        res = await client.post(f"{BASE}/import", json=_payload(src, dry_run=True))
        assert res.status_code == 202
        task = res.json()
        assert (await _wait(client, task["poll_url"]))["status"] == "COMPLETED"
        result = (await client.get(f"{BASE}/result/{task['task_id']}")).json()
    assert result["dry_run"] is True
    assert result["import_plan"]["rows_new"] == 2
    assert result["partitions"][0]["result"] == "planned"
    assert tree_fingerprint(store) == before


async def test_missing_source_is_404_before_creating_a_task(store: Path, tmp_path: Path) -> None:
    async with _client() as client:
        res = await client.post(
            f"{BASE}/import", json=_payload(tmp_path / "nope.csv", confirm=True)
        )
    assert res.status_code == 404


async def test_source_inside_store_is_400(store: Path) -> None:
    inside = store / "binance" / "x.parquet"
    make_ohlcv(hours(JAN_2024_MS, 2)).write_parquet(inside)
    async with _client() as client:
        res = await client.post(f"{BASE}/import", json=_payload(inside, confirm=True))
    assert res.status_code == 400


async def test_missing_destination_without_create_is_404(parquet_dir: Path, tmp_path: Path) -> None:
    src = write_source(tmp_path / "incoming", make_binance(hours(JAN_2024_MS, 2)), "parquet")
    async with _client() as client:
        res = await client.post(f"{BASE}/import", json=_payload(src, confirm=True))
        created = await client.post(f"{BASE}/import", json=_payload(src, confirm=True, create=True))
        assert created.status_code == 202
        await _wait(client, created.json()["poll_url"])
    assert res.status_code == 404
    assert read_dataset(parquet_dir / "binance").height == 2


async def test_invalid_destination_is_400(store: Path, tmp_path: Path) -> None:
    src = _new_source(tmp_path)
    async with _client() as client:
        res = await client.post(
            f"{BASE}/import", json=_payload(src, confirm=True, timeframe="7x", create=True)
        )
    assert res.status_code == 400


async def test_invalid_body_is_422(store: Path) -> None:
    async with _client() as client:
        res = await client.post(f"{BASE}/import", json={"confirm": True})
    assert res.status_code == 422


async def test_invalid_file_fails_the_task_and_result_is_422(store: Path, tmp_path: Path) -> None:
    bad = write_source(
        tmp_path / "incoming", make_ohlcv(hours(JAN_2024_MS, 3)).drop("volume"), "csv"
    )
    before = tree_fingerprint(store)
    async with _client() as client:
        res = await client.post(f"{BASE}/import", json=_payload(bad, confirm=True))
        status = await _wait(client, res.json()["poll_url"])
        result = await client.get(f"{BASE}/result/{res.json()['task_id']}")
    assert status["status"] == "FAILED"
    assert "volume" in status["errors_json"][0]
    assert result.status_code == 422
    assert tree_fingerprint(store) == before


async def test_conflict_with_fail_policy_fails_the_task_and_result_is_409(
    store: Path, tmp_path: Path
) -> None:
    src = write_source(tmp_path / "incoming", make_ohlcv(hours(JAN_2024_MS, 2)), "parquet")
    before = tree_fingerprint(store)
    async with _client() as client:
        res = await client.post(
            f"{BASE}/import", json=_payload(src, confirm=True, on_conflict="fail")
        )
        status = await _wait(client, res.json()["poll_url"])
        result = await client.get(f"{BASE}/result/{res.json()['task_id']}")
    assert status["status"] == "FAILED"
    assert result.status_code == 409
    assert "conflictos" in result.json()["detail"]
    assert tree_fingerprint(store) == before


async def test_replace_and_skip_invalid_through_the_api(store: Path, tmp_path: Path) -> None:
    df = make_ohlcv(hours(JAN_2024_MS + 8 * 3_600_000, 2)).with_columns(
        close=pl.Series([100.0, -5.0], dtype=pl.Float32)
    )
    src = write_source(tmp_path / "incoming", df, "jsonl")
    async with _client() as client:
        res = await client.post(
            f"{BASE}/import", json=_payload(src, confirm=True, skip_invalid=True)
        )
        await _wait(client, res.json()["poll_url"])
        plan = (await client.get(f"{BASE}/result/{res.json()['task_id']}")).json()["import_plan"]
    assert (plan["rows_new"], plan["rows_rejected"]) == (1, 1)
    assert len(plan["rejected_examples"]) == 1
