"""T4.4: router FastAPI ``/ta-lib`` (202 inmediato, 400/404/409, export en *threadpool*)."""

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
from finances_core.schemas.market_data import TF_MS_MAP
from finances_core.schemas.ta_lib import TaLibComputeRequest, TaLibSpec

from finances_analytics.ft_ta_lib import api, service

from .conftest import JAN_2024_MS, DatasetFactory

BASE = "/ta-lib"
UNKNOWN = "0192f000-0000-7000-8000-0000000000ff"
HOUR = TF_MS_MAP["1h"]
DS_PATH = "binance/spot/BTCUSDT/1h"
EXPORT = f"{BASE}/datasets/{DS_PATH}/export"


def _body(
    *functions: dict[str, Any], groups: list[str] | None = None, datasets: list[str] | None = None
) -> dict[str, Any]:
    parsed = [DatasetIdentifier.parse(d).model_dump() for d in (datasets or [DS_PATH])]
    body: dict[str, Any] = {"datasets": parsed, "functions": list(functions)}
    if groups is not None:
        body["groups"] = groups
    elif not functions:
        body["functions"] = [{"function": "rsi"}]
    return body


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


def _computed(client: TestClient, *functions: dict[str, Any]) -> str:
    response = client.post(f"{BASE}/compute", json=_body(*functions))
    assert response.status_code == 202, response.text
    task_id: str = response.json()["task_id"]
    return task_id


def test_router_exposes_plan_routes_with_unique_names() -> None:
    routes = [r for r in api.router.routes if isinstance(r, APIRoute)]
    paths = {(r.path, tuple(sorted(r.methods))) for r in routes}
    dataset = f"{BASE}/datasets/{{exchange}}/{{market_type}}/{{symbol}}/{{timeframe}}"
    assert paths == {
        (f"{BASE}/catalog", ("GET",)),
        (f"{BASE}/compute", ("POST",)),
        (f"{BASE}/status/{{task_id}}", ("GET",)),
        (f"{BASE}/plan/{{task_id}}", ("GET",)),
        (f"{BASE}/result/{{task_id}}", ("GET",)),
        (dataset, ("GET",)),
        (f"{dataset}/export", ("POST",)),
    }
    names = sorted(r.name for r in routes)
    assert names == sorted(
        f"ta_lib_{a}"
        for a in ("catalog", "compute", "status", "plan", "result", "manifest", "export")
    )


def test_catalog_and_filters(client: TestClient) -> None:
    response = client.get(f"{BASE}/catalog")
    assert response.status_code == 200
    assert response.json() == service.catalog().model_dump(mode="json")
    patterns = client.get(f"{BASE}/catalog", params={"group": "Pattern Recognition"}).json()
    assert len(patterns["functions"]) == 61
    assert {e["group"] for e in patterns["functions"]} == {"Pattern Recognition"}
    assert list(patterns["groups"]) == ["Pattern Recognition"]
    rsi = client.get(f"{BASE}/catalog", params={"function": "rsi"}).json()
    assert [e["function"] for e in rsi["functions"]] == ["RSI"]
    for params in ({"group": "Nope"}, {"function": "NOPE"}):
        bad = client.get(f"{BASE}/catalog", params=params)
        assert bad.status_code == 400
        assert "Pattern Recognition" in bad.text


def test_post_compute_returns_202_fast_without_computing(
    client: TestClient,
    ds: DatasetIdentifier,
    settings: FinancesSettings,
    dispatched: list[str],
) -> None:
    client.get(f"{BASE}/catalog")  # calentamiento de la app y del catálogo
    started = time.perf_counter()
    response = client.post(f"{BASE}/compute", json=_body({"function": "rsi"}, {"function": "macd"}))
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 202, response.text
    assert elapsed_ms <= 200, elapsed_ms  # Art. 18 / RF-12
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
    response = client.post(
        f"{BASE}/compute", json=_body({"function": "kama"}, groups=["Pattern Recognition"])
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    status: dict[str, Any] = {}
    for _ in range(100):
        status = client.get(f"{BASE}/status/{task_id}").json()
        if status["status"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.05)
    assert status["status"] == "COMPLETED"
    assert status["items_done"] == 62
    assert f"{DS_PATH}:talib_kama_30" in status["stability"]
    plan = client.get(f"{BASE}/plan/{task_id}").json()
    assert plan["items"][0]["series_id"] == "talib_kama_30"
    assert plan["items"][0]["columns"] == {"talib_kama_30": "Float32"}
    result = client.get(f"{BASE}/result/{task_id}").json()
    assert result["status"] == "COMPLETED"
    assert len(result["items"]) == 62
    manifest = client.get(f"{BASE}/datasets/{DS_PATH}")
    assert manifest.status_code == 200
    assert manifest.json()["namespace"] == "ft_ta_lib"
    assert manifest.json()["entries"]["talib_cdlengulfing"]["column_dtypes"] == {
        "talib_cdlengulfing": "Int16"
    }


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (_body({"function": "nope"}), "Pattern Recognition"),
        (_body(groups=["Nope"]), "Momentum Indicators"),
        (_body({"function": "mavp"}), "unavailable_input"),
        (_body({"function": "rsi", "params": {"timeperiod": 1}}), "TA_BAD_PARAM"),
        (_body({"function": "rsi", "params": {"nope": 1}}), "nope"),
        (_body({"function": "rsi", "params": {"timeperiod": True}}), "timeperiod"),
        (_body({"function": "sma", "inputs": {"price": "adj_close"}}), "adj_close"),
        (_body({"function": "atr", "inputs": {"prices": "open"}}), "prices"),
        (_body({"function": "bbands", "params": {"matype": "nope"}}), "matype"),
        ({"datasets": [], "functions": [{"function": "rsi"}]}, "datasets"),
        ({"functions": [{"function": "rsi"}]}, "datasets"),
        (
            {"datasets": [DatasetIdentifier.parse(DS_PATH).model_dump()]},
            "functions o groups",
        ),
        (
            {
                "datasets": [
                    {"exchange": "binance", "market_type": "bad", "symbol": "X", "timeframe": "1h"}
                ],
                "functions": [{"function": "rsi"}],
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
        TaLibComputeRequest(datasets=[ds], functions=[TaLibSpec(function="rsi")]), settings
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


def test_invalid_task_id_is_400(client: TestClient) -> None:
    assert client.get(f"{BASE}/status/not-a-task").status_code == 400


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
    _computed(client, {"function": "rsi"})
    response = client.post(EXPORT, json={"format": fmt, "include_ohlcv": True})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(media_type)
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="binance_spot_BTCUSDT_1h_ta_lib.{fmt}"'
    )
    assert "x-ft-indicators-stale" not in response.headers
    assert response.content


def test_export_parquet_keeps_int16(client: TestClient, ds: DatasetIdentifier) -> None:
    _computed(client, {"function": "cdlengulfing"}, {"function": "maxindex"})
    response = client.post(EXPORT, json={"format": "parquet"})
    assert response.status_code == 200, response.text
    frame = pl.read_parquet(response.content)
    assert frame.schema["talib_cdlengulfing"] == pl.Int16
    assert frame.schema["talib_maxindex_30"] == pl.Int64


def test_export_csv_content_and_stale_header(
    client: TestClient, ds: DatasetIdentifier, hive_dataset: DatasetFactory
) -> None:
    _computed(client, {"function": "rsi"}, {"function": "ema"})
    hive_dataset(rows=2, start_ms=JAN_2024_MS + 300 * HOUR, seed=3)
    response = client.post(
        EXPORT, json={"format": "csv", "series": ["rsi"], "end_ms": JAN_2024_MS + 50 * HOUR}
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-ft-indicators-stale"] == "talib_rsi_14"
    frame = pl.read_csv(response.content)
    assert frame.height == 50
    assert frame.columns[-1] == "talib_rsi_14"


def test_export_to_output_dir(client: TestClient, ds: DatasetIdentifier, tmp_path: Path) -> None:
    _computed(client, {"function": "rsi"})
    out = tmp_path / "exports"
    response = client.post(EXPORT, json={"format": "csv", "output_dir": str(out)})
    assert response.status_code == 200, response.text
    assert (out / "binance_spot_BTCUSDT_1h_ta_lib.csv").is_file()


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"stability": "drop"}, 400),  # rsi: unstable_period_unknown
        ({"series": ["ema"]}, 404),
        ({"start_ms": JAN_2024_MS + 10_000 * HOUR}, 400),
        ({"format": "xlsx"}, 400),
        ({"series": ["rsi_14"]}, 400),  # serie de ft_indicators
    ],
)
def test_export_errors(
    client: TestClient, ds: DatasetIdentifier, body: dict[str, Any], code: int
) -> None:
    _computed(client, {"function": "rsi"})
    response = client.post(EXPORT, json=body)
    assert response.status_code == code, response.text


def test_export_into_data_dir_is_400_and_unwritable_is_500(
    client: TestClient, ds: DatasetIdentifier, settings: FinancesSettings, tmp_path: Path
) -> None:
    _computed(client, {"function": "rsi"})
    forbidden = client.post(EXPORT, json={"output_dir": str(settings.PARQUET_DIR)})
    assert forbidden.status_code == 400
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert client.post(EXPORT, json={"output_dir": str(blocker)}).status_code == 500


def test_export_without_computation_is_404(client: TestClient, ds: DatasetIdentifier) -> None:
    response = client.post(EXPORT, json={})
    assert response.status_code == 404
    assert "compute" in response.text
