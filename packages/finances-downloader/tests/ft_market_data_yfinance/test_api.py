"""T4.3 — Router FastAPI ``/market-data/yfinance`` (202 inmediato, 404/409, sin bloquear)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data_yfinance import YFDownloadRequest

from finances_downloader.ft_market_data_yfinance import api
from finances_downloader.ft_market_data_yfinance.service import create_download_task

from .conftest import FakeYahoo, business_days, ms, yahoo_frame

BASE = "/market-data/yfinance"
UNKNOWN = "0192f000-0000-7000-8000-0000000000ff"
PARAMS: dict[str, Any] = {
    "symbols": ["AAPL"],
    "timeframes": ["1d"],
    "start": "2024-01-01",
    "end": "2024-03-01",
}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[YFDownloadRequest, str]]:
    """Sustituye la ejecución en segundo plano para comprobar el despacho inmediato."""
    calls: list[tuple[YFDownloadRequest, str]] = []

    def fake(request: YFDownloadRequest, task_id: str) -> None:
        calls.append((request, task_id))

    monkeypatch.setattr(api, "run_download_sync", fake)
    return calls


def test_router_exposes_four_endpoints() -> None:
    paths = {
        (r.path, tuple(sorted(r.methods))) for r in api.router.routes if isinstance(r, APIRoute)
    }
    assert paths == {
        (f"{BASE}/download", ("POST",)),
        (f"{BASE}/status/{{task_id}}", ("GET",)),
        (f"{BASE}/plan/{{task_id}}", ("GET",)),
        (f"{BASE}/result/{{task_id}}", ("GET",)),
    }


def test_post_returns_202_immediately_with_urls(
    client: TestClient, dispatched: list[tuple[YFDownloadRequest, str]]
) -> None:
    response = client.post(f"{BASE}/download", params={**PARAMS, "max_concurrent": 2})
    assert response.status_code == 202
    body = response.json()
    task_id = body["task_id"]
    assert body["status"] == "ACCEPTED"
    assert body["poll_url"].endswith(f"{BASE}/status/{task_id}")
    assert body["plan_url"].endswith(f"{BASE}/plan/{task_id}")
    assert body["result_url"].endswith(f"{BASE}/result/{task_id}")
    ((request, dispatched_id),) = dispatched
    assert dispatched_id == task_id
    assert request.start_ms == ms(2024, 1, 1)
    assert request.max_concurrent == 2
    # La tarea existe (PENDING) antes de planificar: status nunca da 404.
    status = client.get(f"{BASE}/status/{task_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "PENDING"


def test_post_accepts_ms_epoch_and_verbatim_symbols(
    client: TestClient, dispatched: list[tuple[YFDownloadRequest, str]]
) -> None:
    response = client.post(
        f"{BASE}/download",
        params={
            "symbols": ["^GSPC", "EURUSD=X"],
            "timeframes": ["1M"],
            "start": str(ms(2020, 1, 1)),
        },
    )
    assert response.status_code == 202, response.text
    ((request, _),) = dispatched
    assert request.symbols == ["^GSPC", "EURUSD=X"]
    assert request.start_ms == ms(2020, 1, 1)
    assert request.end_ms is None


@pytest.mark.parametrize(
    "params",
    [
        {**PARAMS, "timeframes": ["4h"]},
        {**PARAMS, "start": "2999-01-01", "end": None},
        {**PARAMS, "start": "ayer"},
        {**PARAMS, "start": "2024-03-01", "end": "2024-01-01"},
    ],
)
def test_post_invalid_request_is_400(
    client: TestClient,
    dispatched: list[tuple[YFDownloadRequest, str]],
    params: dict[str, Any],
) -> None:
    clean = {k: v for k, v in params.items() if v is not None}
    response = client.post(f"{BASE}/download", params=clean)
    assert response.status_code == 400, response.text
    assert dispatched == []


def test_polling_reaches_completed(client: TestClient, use_fake_yahoo: FakeYahoo) -> None:
    use_fake_yahoo.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-02-29")))
    task_id = client.post(f"{BASE}/download", params=PARAMS).json()["task_id"]
    status = client.get(f"{BASE}/status/{task_id}").json()
    assert status["status"] == "COMPLETED"
    assert status["items_done"] == status["items_total"] == 1
    result = client.get(f"{BASE}/result/{task_id}")
    assert result.status_code == 200
    assert result.json()["stats"]["AAPL/1d"]["candles"] == 43
    plan = client.get(f"{BASE}/plan/{task_id}")
    assert plan.status_code == 200
    assert plan.json()["items"][0]["yahoo_interval"] == "1d"


def test_failed_result_is_200_with_errors(client: TestClient, use_fake_yahoo: FakeYahoo) -> None:
    use_fake_yahoo.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-02-29")))
    use_fake_yahoo.rate_limited["AAPL"] = -1
    task_id = client.post(f"{BASE}/download", params=PARAMS).json()["task_id"]
    result = client.get(f"{BASE}/result/{task_id}")
    assert result.status_code == 200
    assert result.json()["status"] == "FAILED"
    assert result.json()["errors"]


@pytest.mark.parametrize("endpoint", ["status", "plan", "result"])
def test_unknown_task_is_404(client: TestClient, endpoint: str) -> None:
    assert client.get(f"{BASE}/{endpoint}/{UNKNOWN}").status_code == 404


def test_invalid_task_id_is_422(client: TestClient) -> None:
    assert client.get(f"{BASE}/status/not-a-uuid").status_code == 422


def test_result_of_unfinished_task_is_409(client: TestClient, settings: FinancesSettings) -> None:
    task_id = "0192f000-0000-7000-8000-0000000000d1"
    request = YFDownloadRequest(symbols=["AAPL"], timeframes=["1d"], start_ms=1, end_ms=2)
    create_download_task(task_id, request, settings=settings)
    response = client.get(f"{BASE}/result/{task_id}")
    assert response.status_code == 409
    assert "PENDING" in response.json()["detail"]


def test_get_endpoints_offload_blocking_io(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, settings: FinancesSettings
) -> None:
    offloaded: list[str] = []
    original = api.run_in_threadpool

    async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(func.__name__)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(api, "run_in_threadpool", spy)
    for endpoint in ("status", "plan", "result"):
        client.get(f"{BASE}/{endpoint}/{UNKNOWN}")
    assert offloaded == ["get_progress", "load_plan", "get_result"]
