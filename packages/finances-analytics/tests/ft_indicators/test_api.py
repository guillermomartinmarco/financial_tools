"""T5.4: router FastAPI ``/indicators`` (202 inmediato, 400/404/409, export en *threadpool*)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import IndicatorComputeRequest, IndicatorSpec
from finances_core.schemas.market_data import TF_MS_MAP

from finances_analytics.ft_indicators import api, service

from .conftest import JAN_2024_MS, DatasetFactory

BASE = "/indicators"
UNKNOWN = "0192f000-0000-7000-8000-0000000000ff"
HOUR = TF_MS_MAP["1h"]
DS_PATH = "binance/spot/BTCUSDT/1h"
EXPORT = f"{BASE}/datasets/{DS_PATH}/export"


def _body(*indicators: dict[str, Any], datasets: list[str] | None = None) -> dict[str, Any]:
    parsed = [DatasetIdentifier.parse(d).model_dump() for d in (datasets or [DS_PATH])]
    return {"datasets": parsed, "indicators": list(indicators) or [{"name": "rsi"}]}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


@pytest.fixture
def ds(hive_dataset: DatasetFactory) -> DatasetIdentifier:
    return hive_dataset(rows=300)


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Sustituye la ejecución en segundo plano para comprobar el despacho inmediato."""
    calls: list[str] = []

    def fake(prepared: service.PreparedCompute, _settings: object = None) -> None:
        calls.append(prepared.task_id)

    monkeypatch.setattr(service, "run_compute_sync", fake)
    return calls


def _computed(client: TestClient, *indicators: dict[str, Any]) -> str:
    response = client.post(f"{BASE}/compute", json=_body(*indicators))
    assert response.status_code == 202, response.text
    task_id: str = response.json()["task_id"]
    return task_id


def test_router_exposes_plan_routes() -> None:
    paths = {
        (r.path, tuple(sorted(r.methods))) for r in api.router.routes if isinstance(r, APIRoute)
    }
    assert paths == {
        (f"{BASE}/catalog", ("GET",)),
        (f"{BASE}/compute", ("POST",)),
        (f"{BASE}/status/{{task_id}}", ("GET",)),
        (f"{BASE}/plan/{{task_id}}", ("GET",)),
        (f"{BASE}/result/{{task_id}}", ("GET",)),
        (f"{BASE}/datasets/{{exchange}}/{{market_type}}/{{symbol}}/{{timeframe}}", ("GET",)),
        (
            f"{BASE}/datasets/{{exchange}}/{{market_type}}/{{symbol}}/{{timeframe}}/export",
            ("POST",),
        ),
    }


def test_catalog(client: TestClient) -> None:
    response = client.get(f"{BASE}/catalog")
    assert response.status_code == 200
    assert response.json() == [e.model_dump(mode="json") for e in service.catalog()]


def test_post_compute_returns_202_fast_without_computing(
    client: TestClient,
    ds: DatasetIdentifier,
    settings: FinancesSettings,
    dispatched: list[str],
) -> None:
    client.get(f"{BASE}/catalog")  # calentamiento de la app
    started = time.perf_counter()
    response = client.post(f"{BASE}/compute", json=_body({"name": "rsi"}, {"name": "macd"}))
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 202, response.text
    assert elapsed_ms <= 200, elapsed_ms  # Art. 18
    body = response.json()
    task_id = body["task_id"]
    assert uuid.UUID(task_id).version == 7
    assert body["status"] == "ACCEPTED"
    assert body["poll_url"] == f"http://testserver{BASE}/status/{task_id}"
    assert body["plan_url"] == f"http://testserver{BASE}/plan/{task_id}"
    assert body["result_url"] == f"http://testserver{BASE}/result/{task_id}"
    assert dispatched == [task_id]
    assert not settings.INDICATORS_DIR.exists()  # nada calculado en el endpoint
    status = client.get(f"{BASE}/status/{task_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "PENDING"
    assert client.get(f"{BASE}/result/{task_id}").status_code == 409


def test_polling_until_completed(client: TestClient, ds: DatasetIdentifier) -> None:
    task_id = _computed(client, {"name": "ema"}, {"name": "obv"})
    status: dict[str, Any] = {}
    for _ in range(50):
        status = client.get(f"{BASE}/status/{task_id}").json()
        if status["status"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.05)
    assert status["status"] == "COMPLETED"
    assert status["items_done"] == 2
    assert f"{DS_PATH}:ema_20" in status["stability"]
    plan = client.get(f"{BASE}/plan/{task_id}").json()
    assert [i["indicator_id"] for i in plan["items"]] == ["ema_20", "obv"]
    result = client.get(f"{BASE}/result/{task_id}").json()
    assert result["status"] == "COMPLETED"
    assert [i["series_id"] for i in result["items"]] == ["ema_20", "obv"]
    manifest = client.get(f"{BASE}/datasets/{DS_PATH}")
    assert manifest.status_code == 200
    assert manifest.json()["namespace"] == "ft_indicators"
    assert set(manifest.json()["entries"]) == {"ema_20", "obv"}


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (_body({"name": "nope"}), "vwap"),
        (_body({"name": "rsi", "params": {"period": 1}}), "period"),
        (_body({"name": "atr", "params": {"source": "open"}}), "source"),
        (_body({"name": "sma", "params": {"source": "adj_close"}}), "adj_close"),
        (_body({"name": "macd", "params": {"fast": 30}}), "fast"),
        ({"datasets": [], "indicators": [{"name": "rsi"}]}, "datasets"),
        ({"indicators": [{"name": "rsi"}]}, "datasets"),
        (
            {
                "datasets": [
                    {"exchange": "binance", "market_type": "bad", "symbol": "X", "timeframe": "1h"}
                ],
                "indicators": [{"name": "rsi"}],
            },
            "market_type",
        ),
    ],
)
def test_post_compute_validation_is_400(
    client: TestClient, ds: DatasetIdentifier, body: dict[str, Any], fragment: str
) -> None:
    response = client.post(f"{BASE}/compute", json=body)
    assert response.status_code == 400, response.text
    assert fragment in response.text


def test_post_compute_unknown_dataset_is_404(client: TestClient, ds: DatasetIdentifier) -> None:
    response = client.post(f"{BASE}/compute", json=_body(datasets=["binance/spot/NOPE/1h"]))
    assert response.status_code == 404


def test_post_compute_locked_dataset_is_409(
    client: TestClient, ds: DatasetIdentifier, settings: FinancesSettings
) -> None:
    owner = service.dispatch_compute(
        IndicatorComputeRequest(datasets=[ds], indicators=[IndicatorSpec(name="rsi")]), settings
    )
    with service.get_store(settings).lock(ds, owner.task_id):
        response = client.post(f"{BASE}/compute", json=_body())
    assert response.status_code == 409
    assert owner.task_id in response.text


def test_indicators_dir_inside_parquet_dir_is_500(
    client: TestClient,
    ds: DatasetIdentifier,
    settings: FinancesSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FT_INDICATORS_DIR", str(settings.PARQUET_DIR / "ind"))
    assert client.post(f"{BASE}/compute", json=_body()).status_code == 500


@pytest.mark.parametrize("route", ["status", "plan", "result"])
def test_unknown_task_is_404(client: TestClient, route: str) -> None:
    assert client.get(f"{BASE}/{route}/{UNKNOWN}").status_code == 404


def test_manifest_not_found_and_invalid_dataset(client: TestClient, ds: DatasetIdentifier) -> None:
    assert client.get(f"{BASE}/datasets/{DS_PATH}").status_code == 404
    assert client.get(f"{BASE}/datasets/binance/bad/BTCUSDT/1h").status_code == 400


# ------------------------------------------------------------------- export
@pytest.mark.parametrize(
    ("fmt", "media_type"),
    [
        ("parquet", "application/vnd.apache.parquet"),
        ("csv", "text/csv"),
        ("jsonl", "application/x-ndjson"),
    ],
)
def test_export_streams_file(
    client: TestClient, ds: DatasetIdentifier, fmt: str, media_type: str
) -> None:
    _computed(client, {"name": "rsi"})
    response = client.post(EXPORT, json={"format": fmt, "include_ohlcv": True})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(media_type)
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="binance_spot_BTCUSDT_1h_indicators.{fmt}"'
    )
    assert "x-ft-indicators-stale" not in response.headers
    assert response.content


def test_export_csv_content_and_stale_header(
    client: TestClient, ds: DatasetIdentifier, hive_dataset: DatasetFactory
) -> None:
    _computed(client, {"name": "rsi"}, {"name": "ema"})
    hive_dataset(rows=2, start_ms=JAN_2024_MS + 300 * HOUR, seed=3)
    response = client.post(
        EXPORT, json={"format": "csv", "series": ["rsi"], "end_ms": JAN_2024_MS + 50 * HOUR}
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-ft-indicators-stale"] == "rsi_14"
    frame = pl.read_csv(response.content)
    assert frame.height == 50
    assert frame.columns[-1] == "rsi_14"


def test_export_to_output_dir(client: TestClient, ds: DatasetIdentifier, tmp_path: Path) -> None:
    _computed(client, {"name": "rsi"})
    out = tmp_path / "exports"
    response = client.post(EXPORT, json={"format": "csv", "output_dir": str(out)})
    assert response.status_code == 200, response.text
    assert (out / "binance_spot_BTCUSDT_1h_indicators.csv").is_file()


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"stability": "drop"}, 400),  # obv sin stable_from_ms
        ({"series": ["ema"]}, 404),
        ({"start_ms": JAN_2024_MS + 10_000 * HOUR}, 400),
        ({"format": "xlsx"}, 400),
        ({"series": ["talib_rsi_14"]}, 400),
    ],
)
def test_export_errors(
    client: TestClient, ds: DatasetIdentifier, body: dict[str, Any], code: int
) -> None:
    _computed(client, {"name": "obv"})
    response = client.post(EXPORT, json=body)
    assert response.status_code == code, response.text


def test_export_into_data_dir_is_400_and_unwritable_is_500(
    client: TestClient, ds: DatasetIdentifier, settings: FinancesSettings, tmp_path: Path
) -> None:
    _computed(client, {"name": "rsi"})
    forbidden = client.post(EXPORT, json={"output_dir": str(settings.PARQUET_DIR)})
    assert forbidden.status_code == 400
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert client.post(EXPORT, json={"output_dir": str(blocker)}).status_code == 500


def test_export_without_computation_is_404(client: TestClient, ds: DatasetIdentifier) -> None:
    response = client.post(EXPORT, json={})
    assert response.status_code == 404
    assert "compute" in response.text
