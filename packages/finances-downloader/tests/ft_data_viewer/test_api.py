"""T6.1–T6.5: router FastAPI (montado en una app de test, sin servidor propio)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import polars as pl
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from finances_downloader.ft_data_viewer import api as api_module
from finances_downloader.ft_data_viewer.api import router

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv, write_dataset

BASE = "/data/datasets/binance/spot/BTCUSDT/1h"
START = JAN_2024_MS + 30 * 24 * HOUR_MS
BTC_ID = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(_app(), raise_server_exceptions=False) as c:
        yield c


def test_openapi_exposes_seven_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/data/datasets",
        "/data/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/summary",
        "/data/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/rows",
        "/data/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/gaps",
        "/data/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/anomalies",
        "/data/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export",
        "/data/compare",
    }
    assert set(paths) == expected
    gaps_schema = paths["/data/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/gaps"]
    ok = gaps_schema["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["type"] == "array"
    assert ok["items"]["$ref"].endswith("/GapInfo")


def test_list_datasets(client: TestClient, parquet_dir: Path, btc_dataset: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(5), market_type="futures", symbol="ETHUSDT")
    body = client.get("/data/datasets", params={"symbol": "ETHUSDT"}).json()
    assert [d["id"]["symbol"] for d in body["datasets"]] == ["ETHUSDT"]
    assert client.get("/data/datasets").json()["total_count"] == 2


def test_summary_ok_and_404(client: TestClient, btc_dataset: Path) -> None:
    ok = client.get(f"{BASE}/summary")
    assert ok.status_code == 200
    assert ok.json()["row_count"] == 48
    missing = client.get("/data/datasets/binance/spot/NOPE/1h/summary")
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "/data/datasets/binance/options/BTCUSDT/1h/summary",
        "/data/datasets/binance/spot/BTCUSDT/7h/gaps",
        f"{BASE}/rows?cols=nope",
    ],
)
def test_invalid_arguments_are_400(client: TestClient, btc_dataset: Path, url: str) -> None:
    assert client.get(url).status_code == 400


def test_validation_errors_are_422(client: TestClient, btc_dataset: Path) -> None:
    assert client.get(f"{BASE}/rows", params={"n": 0}).status_code == 422
    assert client.get(f"{BASE}/rows", params={"mode": "middle"}).status_code == 422


def test_internal_error_is_500(
    client: TestClient, btc_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(api_module.service, "get_summary", boom)
    assert client.get(f"{BASE}/summary").status_code == 500


def test_handlers_run_in_threadpool(
    client: TestClient, btc_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Art. 3: las llamadas Polars se despachan a run_in_threadpool."""
    calls: list[str] = []
    original = api_module.run_in_threadpool

    async def spy(func: object, *args: object, **kwargs: object) -> object:
        calls.append(getattr(func, "__name__", "?"))
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(api_module, "run_in_threadpool", spy)
    for url in (
        "/data/datasets",
        f"{BASE}/summary",
        f"{BASE}/rows",
        f"{BASE}/gaps",
        f"{BASE}/anomalies",
    ):
        assert client.get(url).status_code == 200
    assert len(calls) == 5


# --- T6.2 /rows -----------------------------------------------------------------------


def test_rows_modes(client: TestClient, btc_dataset: Path) -> None:
    head = client.get(f"{BASE}/rows", params={"mode": "head", "n": 2}).json()
    assert [r["open_time_ms"] for r in head] == [START, START + HOUR_MS]
    tail = client.get(f"{BASE}/rows", params={"mode": "tail", "n": 1}).json()
    assert tail[0]["open_time_ms"] == START + 47 * HOUR_MS
    sample = client.get(f"{BASE}/rows", params={"mode": "sample", "n": 5, "cols": "close"}).json()
    assert len(sample) == 5
    assert set(sample[0]) == {"close"}


def test_rows_404(client: TestClient, btc_dataset: Path) -> None:
    assert client.get("/data/datasets/binance/spot/NOPE/1h/rows").status_code == 404


# --- gaps / anomalies -----------------------------------------------------------------


def test_gaps_and_anomalies(client: TestClient, parquet_dir: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(30, drop=[4, 5, 9]))
    gaps = client.get(f"{BASE}/gaps").json()
    assert [g["missing_candles"] for g in gaps] == [2, 1]
    assert [g["missing_candles"] for g in client.get(f"{BASE}/gaps?min_gap_candles=2").json()] == [
        2
    ]
    anomalies = client.get(f"{BASE}/anomalies").json()
    assert anomalies["dataset_id"] == BTC_ID
    assert client.get(f"{BASE}/anomalies?outlier_method=iqr").status_code == 200


# --- T6.3 /export ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "media"),
    [
        ("parquet", "application/vnd.apache.parquet"),
        ("csv", "text/csv"),
        ("jsonl", "application/x-ndjson"),
    ],
)
def test_export_streams_download(
    client: TestClient, btc_dataset: Path, fmt: str, media: str
) -> None:
    body = {"dataset_id": BTC_ID, "format": fmt, "start_ms": START, "end_ms": START + 4 * HOUR_MS}
    res = client.post(f"{BASE}/export", json=body)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith(media)
    assert res.headers["content-disposition"] == (
        f'attachment; filename="binance_spot_BTCUSDT_1h.{fmt}"'
    )
    if fmt == "parquet":
        df = pl.read_parquet(io.BytesIO(res.content))
    elif fmt == "csv":
        df = pl.read_csv(io.BytesIO(res.content))
    else:
        df = pl.DataFrame([json.loads(line) for line in res.text.splitlines()])
    assert df.height == 5


def test_export_with_output_dir_writes_and_streams(
    client: TestClient, btc_dataset: Path, tmp_path: Path
) -> None:
    out = tmp_path / "exports"
    body = {"dataset_id": BTC_ID, "format": "csv", "output_dir": str(out)}
    res = client.post(f"{BASE}/export", json=body)
    assert res.status_code == 200
    assert (out / "binance_spot_BTCUSDT_1h.csv").is_file()
    assert len(res.text.splitlines()) == 49


def test_export_temp_file_is_removed(
    client: TestClient, btc_dataset: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[Path] = []
    original = api_module.tempfile.mkdtemp

    def spy(*a: object, **k: object) -> str:
        path: str = original(dir=tmp_path)
        created.append(Path(path))
        return path

    monkeypatch.setattr(api_module.tempfile, "mkdtemp", spy)
    res = client.post(f"{BASE}/export", json={"dataset_id": BTC_ID, "format": "csv"})
    assert res.status_code == 200
    assert created
    assert not created[0].exists()


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"dataset_id": BTC_ID, "start_ms": 1, "end_ms": 2}, 400),
        ({"dataset_id": {**BTC_ID, "symbol": "ETHUSDT"}}, 400),
        ({"dataset_id": BTC_ID, "columns": ["nope"]}, 400),
        ({"dataset_id": BTC_ID, "format": "xlsx"}, 422),
    ],
)
def test_export_errors(
    client: TestClient, btc_dataset: Path, body: dict[str, object], status: int
) -> None:
    assert client.post(f"{BASE}/export", json=body).status_code == status


def test_export_404_and_parquet_dir_guard(
    client: TestClient, btc_dataset: Path, parquet_dir: Path
) -> None:
    nope = {**BTC_ID, "symbol": "NOPE"}
    assert (
        client.post(
            "/data/datasets/binance/spot/NOPE/1h/export", json={"dataset_id": nope}
        ).status_code
        == 404
    )
    guarded = client.post(
        f"{BASE}/export", json={"dataset_id": BTC_ID, "output_dir": str(parquet_dir / "x")}
    )
    assert guarded.status_code == 400
    assert "PARQUET_DIR" in guarded.json()["detail"]


# --- T6.4 /compare --------------------------------------------------------------------


def test_compare_endpoint(client: TestClient, parquet_dir: Path, btc_dataset: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(48, start_ms=START, drop=[2]), market_type="futures")
    body = {"dataset_a": BTC_ID, "dataset_b": {**BTC_ID, "market_type": "futures"}}
    res = client.post("/data/compare", json=body)
    assert res.status_code == 200
    assert res.json()["row_count_diff"] == 1
    assert len(res.json()["gaps_only_in_b"]) == 1
    missing = client.post("/data/compare", json={**body, "dataset_b": {**BTC_ID, "symbol": "X"}})
    assert missing.status_code == 404


def test_api_logs_include_task_id(client: TestClient, btc_dataset: Path) -> None:
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        client.get(f"{BASE}/summary")
    (event,) = [e for e in logs if e["event"] == "dataset_operation_complete"]
    assert event["task_id"]
    assert event["dataset_id"] == "binance/spot/BTCUSDT/1h"


# --- T6.5 httpx.AsyncClient -----------------------------------------------------------


async def test_async_client_roundtrip(btc_dataset: Path) -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        listed = await ac.get("/data/datasets")
        summary = await ac.get(f"{BASE}/summary")
        gaps = await ac.get(f"{BASE}/gaps")
    assert listed.json()["total_count"] == 1
    assert summary.json()["row_count"] == 48
    assert gaps.json() == []
