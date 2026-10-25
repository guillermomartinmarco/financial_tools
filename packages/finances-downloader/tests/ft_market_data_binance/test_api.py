"""T6.1-T6.5 — Router FastAPI ``/market-data/binance`` (montado por ``finances-api``)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data import DownloadRequest
from finances_core.tasks import SQLTaskManager

from finances_downloader.ft_market_data_binance import api
from finances_downloader.ft_market_data_binance.service import (
    create_download_task,
    new_task_id,
)

from .conftest import FakeBinance, ms

BASE = "/market-data/binance"
QUERY: dict[str, Any] = {
    "symbols": ["BTCUSDT"],
    "timeframes": ["1h"],
    "start_ms": ms(2024, 3, 1),
    "end_ms": ms(2024, 3, 2),
}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


@pytest.fixture
def no_background(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Sustituye la ejecución en background: registra el estado visible al despachar."""
    seen: list[tuple[str, str | None]] = []

    def fake(
        request: DownloadRequest, task_id: str, settings: FinancesSettings | None = None
    ) -> None:
        row = SQLTaskManager(FinancesSettings().TASKS_DB_PATH).get_task(task_id)
        seen.append((task_id, None if row is None else row["status"]))

    monkeypatch.setattr(api, "run_download_sync", fake)
    return seen


# --------------------------------------------------------------------- T6.1
def test_router_exposes_four_endpoints(client: TestClient) -> None:
    assert api.router.prefix == BASE
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        f"{BASE}/download",
        f"{BASE}/status/{{task_id}}",
        f"{BASE}/plan/{{task_id}}",
        f"{BASE}/result/{{task_id}}",
    }


# --------------------------------------------------------------------- T6.2
def test_post_dispatches_immediately_with_absolute_poll_url(
    client: TestClient,
    no_background: list[tuple[str, str | None]],
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
    fake_binance: FakeBinance,
) -> None:
    started = time.monotonic()
    response = client.post(f"{BASE}/download", params=QUERY)
    elapsed = time.monotonic() - started
    assert response.status_code == 202
    body = response.json()
    task_id = body["task_id"]
    assert uuid.UUID(task_id).version == 7
    assert body["status"] == "ACCEPTED"
    assert body["poll_url"] == f"http://testserver{BASE}/status/{task_id}"
    assert body["result_url"] == f"http://testserver{BASE}/result/{task_id}"
    # La tarea ya existía (PENDING) al lanzar el background y no se hizo red en el endpoint.
    assert no_background == [(task_id, "PENDING")]
    assert fake_binance.requests == []
    assert elapsed < 1.0
    # GET /status nunca da 404 para una tarea despachada
    status = client.get(f"{BASE}/status/{task_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "PENDING"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"start_ms": 32503680000000, "end_ms": None}, "Cannot download future data"),
        ({"timeframes": ["7h"]}, "timeframes"),
        ({"end_ms": ms(2024, 2, 1)}, "end_ms"),
    ],
)
def test_post_invalid_request_is_400(
    client: TestClient,
    no_background: list[tuple[str, str | None]],
    overrides: dict[str, Any],
    fragment: str,
) -> None:
    params = {k: v for k, v in {**QUERY, **overrides}.items() if v is not None}
    response = client.post(f"{BASE}/download", params=params)
    assert response.status_code == 400
    assert fragment in response.json()["detail"]
    assert no_background == []


def test_post_invalid_market_is_422(client: TestClient) -> None:
    response = client.post(f"{BASE}/download", params={**QUERY, "market_type": "options"})
    assert response.status_code == 422


# --------------------------------------------------------------------- T6.3
def test_polling_reaches_completed(
    client: TestClient,
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    real = SQLTaskManager.update_task

    def spy(self: SQLTaskManager, task_id: str, **fields: object) -> None:
        status = fields.get("status")
        if isinstance(status, str) and (not seen or seen[-1] != status):
            seen.append(status)
        real(self, task_id, **fields)

    monkeypatch.setattr(SQLTaskManager, "update_task", spy)
    task_id = client.post(f"{BASE}/download", params=QUERY).json()["task_id"]
    status = client.get(f"{BASE}/status/{task_id}").json()
    assert status["status"] == "COMPLETED"
    assert status["processed_candles"] == 25
    assert status["symbols_total"] == status["symbols_done"] == 1
    assert seen == [
        "PLANNING",
        "DOWNLOADING_PUBLIC",
        "DOWNLOADING_REST",
        "NORMALIZING",
        "WRITING_PARQUET",
        "COMPLETED",
    ]


def test_status_unknown_task_404(client: TestClient) -> None:
    assert client.get(f"{BASE}/status/{new_task_id()}").status_code == 404
    assert client.get(f"{BASE}/status/not-a-uuid").status_code == 422


def test_get_endpoints_offload_blocking_io(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = api.run_in_threadpool

    async def spy(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        calls.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(api, "run_in_threadpool", spy)
    task_id = new_task_id()
    client.get(f"{BASE}/status/{task_id}")
    client.get(f"{BASE}/plan/{task_id}")
    client.get(f"{BASE}/result/{task_id}")
    assert calls == ["get_progress", "load_plan", "get_result"]


# --------------------------------------------------------------------- T6.4
def test_plan_endpoint(
    client: TestClient,
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
) -> None:
    task_id = client.post(f"{BASE}/download", params=QUERY).json()["task_id"]
    plan = client.get(f"{BASE}/plan/{task_id}")
    assert plan.status_code == 200
    assert plan.json()["task_id"] == task_id
    assert plan.json()["items"][0]["source"] == "rest_api"
    assert client.get(f"{BASE}/plan/{new_task_id()}").status_code == 404


# --------------------------------------------------------------------- T6.5
def test_result_completed_with_paths_stats_and_unfillable(
    client: TestClient,
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
    fake_binance: FakeBinance,
) -> None:
    fake_binance.rest_from_ms = ms(2024, 3, 1, 12)
    task_id = client.post(f"{BASE}/download", params=QUERY).json()["task_id"]
    body = client.get(f"{BASE}/result/{task_id}").json()
    assert body["status"] == "COMPLETED"
    assert len(body["parquet_paths"]) == 1
    assert body["stats"]["BTCUSDT/1h/spot"]["candles"] == 13
    assert body["gaps_unfillable"] == []  # REST tiene datos parciales: no es un gap vacío


def test_result_failed_returns_200_with_errors(
    client: TestClient,
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
    fake_binance: FakeBinance,
) -> None:
    fake_binance.forced_status["/api/v3/klines"] = [500] * 10
    task_id = client.post(f"{BASE}/download", params=QUERY).json()["task_id"]
    response = client.get(f"{BASE}/result/{task_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "FAILED"
    assert "500" in body["stats"]["errors"][0]


def test_result_pending_409_and_unknown_404(client: TestClient, settings: FinancesSettings) -> None:
    task_id = new_task_id()
    create_download_task(
        task_id,
        DownloadRequest.model_validate(
            {"symbols": ["BTCUSDT"], "timeframes": ["1h"], "start_ms": 1}
        ),
        settings=settings,
    )
    assert client.get(f"{BASE}/result/{task_id}").status_code == 409
    assert client.get(f"{BASE}/result/{new_task_id()}").status_code == 404


def test_result_unfillable_gap_is_reported(
    client: TestClient,
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
    fake_binance: FakeBinance,
) -> None:
    """Bug #7: ``gaps_unfillable`` era siempre ``[]`` (TODO en api.py)."""
    fake_binance.rest_from_ms = ms(2030, 1, 1)
    task_id = client.post(f"{BASE}/download", params=QUERY).json()["task_id"]
    body = client.get(f"{BASE}/result/{task_id}").json()
    assert body["status"] == "COMPLETED"
    assert body["gaps_unfillable"][0]["range_ms"] == [ms(2024, 3, 1), ms(2024, 3, 2)]
