"""V4.1: router FastAPI ``/backtest/vectorbt`` (``POST /run``, ``GET /status``, ``GET /result``).

Estilo ``httpx.AsyncClient`` + ``ASGITransport`` sobre un ``FastAPI`` desnudo que monta solo
``api.router`` (sin arrastrar ``finances-api``); ``asyncio_mode = "auto"`` está activo. Sin red.

``POST /run`` responde ``202`` sin calcular: el cálculo corre en ``BackgroundTasks`` (Art. 18).
Bajo ``ASGITransport`` las tareas en segundo plano se ejecutan al cerrar la petición, así que
el espía :func:`no_run` sustituye ``service.run_task`` para observar el estado ``PENDING``
inmediato, y el *polling* real espera a ``COMPLETED``. El techo ``DISPATCH_CEILING_MS`` del CI
(deriva de ``ft_indicators`` T5.4) es informativo; el no-cálculo se prueba con el espía.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from finances_core.backtest.errors import StrategyInitError
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import BacktestRequest
from finances_core.tasks import SQLTaskManager

from finances_backtester.ft_vectorbt import api, service
from tests.contract.fixture import GOLDEN_PARAMS

from .conftest import create_strategy, make_request, sma_request

#: Estrategias integradas en orden determinista (igual que la CLI).
BUILTIN_KINDS = ["donchian_breakout", "rsi_reversion", "sma_cross"]
#: run_id SHA-256 válido por forma pero inexistente en el almacén.
UNKNOWN_RUN = "0" * 64
#: número de operaciones del *fixture* ``sma_cross`` con los parámetros de los golden (B6.1).
FIXTURE_TRADES = 55

#: ``task_id``/``strategy_id`` UUID v7 bien formados pero inexistentes.
UNKNOWN = "01890a5d-ac96-774b-bcce-b302099a8001"
BASE = "/backtest/vectorbt"
#: Techo informativo del despacho en CI (Art. 18 ≤ 200 ms es objetivo de producción).
DISPATCH_CEILING_MS = 2000


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


def _body(request: BacktestRequest) -> dict[str, Any]:
    return request.model_dump(mode="json")


@pytest.fixture
def no_run(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Sustituye ``service.run_task`` para observar el despacho inmediato sin calcular."""
    calls: list[str] = []

    async def fake(dispatched: service.DispatchedRun, _settings: object = None) -> None:
        calls.append(dispatched.task_id)

    monkeypatch.setattr(service, "run_task", fake)
    return calls


async def _poll(client: httpx.AsyncClient, task_id: str, *, tries: int = 100) -> dict[str, Any]:
    status: dict[str, Any] = {}
    for _ in range(tries):
        status = (await client.get(f"{BASE}/status/{task_id}")).json()
        if status["status"] in {"COMPLETED", "FAILED"}:
            break
    return status


# ------------------------------------------------------------------- rutas
def test_router_exposes_the_three_v41_routes_with_vectorbt_names() -> None:
    routes = [r for r in api.router.routes if isinstance(r, APIRoute)]
    paths = {(r.path, tuple(sorted(r.methods))) for r in routes}
    assert {
        (f"{BASE}/run", ("POST",)),
        (f"{BASE}/status/{{task_id}}", ("GET",)),
        (f"{BASE}/result/{{task_id}}", ("GET",)),
    } <= paths
    assert {"vectorbt_result", "vectorbt_run", "vectorbt_status"} <= {r.name for r in routes}


# ------------------------------------------------------------- POST /run 202
async def test_post_run_returns_202_with_urls_without_computing(
    service_settings: FinancesSettings, no_run: list[str]
) -> None:
    body = _body(sma_request())
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=body)
        assert response.status_code == 202, response.text
        payload = response.json()
        task_id = payload["task_id"]
        assert uuid.UUID(task_id).version == 7
        assert payload["status"] == "ACCEPTED"
        assert payload["poll_url"] == f"http://testserver{BASE}/status/{task_id}"
        assert payload["result_url"] == f"http://testserver{BASE}/result/{task_id}"
        # El endpoint no calcula: el espía recibió el despacho y la tarea sigue PENDING.
        assert no_run == [task_id]
        status = await client.get(f"{BASE}/status/{task_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "PENDING"
        assert (await client.get(f"{BASE}/result/{task_id}")).status_code == 409


async def test_post_run_does_not_create_a_task_on_disk_before_the_runner(
    service_settings: FinancesSettings, no_run: list[str]
) -> None:
    """La corrida no se escribe en el endpoint: ``RESULTS_DIR`` queda vacío tras el 202."""
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=_body(sma_request()))
    assert response.status_code == 202
    assert list(service_settings.RESULTS_DIR.rglob("*.parquet")) == []


# ------------------------------------------------- polling hasta COMPLETED
async def test_polling_until_completed_then_result(service_settings: FinancesSettings) -> None:
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=_body(sma_request()))
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        status = await _poll(client, task_id)
        assert status["status"] == "COMPLETED", status
        assert status["summary"]["n_trades"] > 0
        result = await client.get(f"{BASE}/result/{task_id}")
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["cached"] is False
        assert data["run_id"] == status["summary"]["run_id"]
        assert data["summary"]["n_trades"] == status["summary"]["n_trades"]


# --------------------------------------------------------- errores RF-9
async def test_result_not_finished_is_409(
    service_settings: FinancesSettings, no_run: list[str]
) -> None:
    async with await _client() as client:
        task_id = (await client.post(f"{BASE}/run", json=_body(sma_request()))).json()["task_id"]
        response = await client.get(f"{BASE}/result/{task_id}")
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("route", ["status", "result"])
async def test_unknown_task_is_404(service_settings: FinancesSettings, route: str) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/{route}/{UNKNOWN}")
    assert response.status_code == 404, response.text


async def test_failed_task_result_is_500(
    service_settings: FinancesSettings, no_run: list[str]
) -> None:
    async with await _client() as client:
        task_id = (await client.post(f"{BASE}/run", json=_body(sma_request()))).json()["task_id"]
        SQLTaskManager(service_settings.TASKS_DB_PATH).update_task(
            task_id, status="FAILED", error="vectorbt exploded"
        )
        response = await client.get(f"{BASE}/result/{task_id}")
    assert response.status_code == 500, response.text
    assert "vectorbt exploded" in response.text


async def test_unknown_strategy_is_404_without_creating_a_task(
    service_settings: FinancesSettings,
) -> None:
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=_body(make_request(UNKNOWN)))
    assert response.status_code == 404, response.text
    assert SQLTaskManager(service_settings.TASKS_DB_PATH).list_tasks() == []


async def test_unknown_kind_is_400(service_settings: FinancesSettings) -> None:
    strategy_id = create_strategy({"kind": "no_such_kind", "params": {}})
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=_body(make_request(strategy_id)))
    assert response.status_code == 400, response.text
    assert SQLTaskManager(service_settings.TASKS_DB_PATH).list_tasks() == []


async def test_invalid_params_is_400(service_settings: FinancesSettings) -> None:
    strategy_id = create_strategy({"kind": "sma_cross", "params": {"bogus_key": 1}})
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=_body(make_request(strategy_id)))
    assert response.status_code == 400, response.text


async def test_strategy_init_error_is_500(
    service_settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_request: BacktestRequest, _settings: object = None) -> service.DispatchedRun:
        raise StrategyInitError("sma_cross", "local", "carpeta inválida")

    monkeypatch.setattr(service, "dispatch", boom)
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=_body(make_request(UNKNOWN)))
    assert response.status_code == 500, response.text


@pytest.mark.parametrize("extra", ["code", "path", "module"])
async def test_request_with_code_or_path_fields_is_422(
    service_settings: FinancesSettings, extra: str
) -> None:
    """``BacktestRequest`` es ``extra='forbid'``: no existe endpoint que reciba código (RF-18)."""
    body = _body(make_request(UNKNOWN))
    body[extra] = "x"
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=body)
    assert response.status_code == 422, response.text


async def test_flexible_start_is_accepted(
    service_settings: FinancesSettings, no_run: list[str]
) -> None:
    """El cuerpo acepta ``start``/``end`` flexibles (ISO 8601, ms epoch o 'now'), RF-7."""
    body = _body(sma_request(start="2024-01-01", end="now"))
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=body)
    assert response.status_code == 202, response.text


async def test_unparseable_start_is_422(service_settings: FinancesSettings) -> None:
    """Un ``start`` no parseable es error de validación de forma (RF-7, 422)."""
    body = _body(sma_request())
    body["start"] = "ayer"
    async with await _client() as client:
        response = await client.post(f"{BASE}/run", json=body)
    assert response.status_code == 422, response.text


async def test_invalid_task_id_in_path_is_422(service_settings: FinancesSettings) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/status/not-a-valid-task-id-at-all")
    assert response.status_code == 422, response.text


# ====================================================================== V4.2: solo lectura
def _make_run(request: BacktestRequest, settings: FinancesSettings) -> str:
    """Deja una corrida completa en disco vía ``run_sync`` (sin red); devuelve su ``run_id``."""
    return service.run_sync(request, settings).run_id


def _rsi_request() -> BacktestRequest:
    """Petición ``rsi_reversion`` (``kind``/``strategy_id`` distintos de ``sma_cross``)."""
    strategy_id = create_strategy(
        {"kind": "rsi_reversion", "params": GOLDEN_PARAMS["rsi_reversion"]}
    )
    return make_request(strategy_id)


# ------------------------------------------------------------------ rutas y nombres V4.2
def test_router_exposes_the_five_v42_read_routes_with_vectorbt_names() -> None:
    routes = [r for r in api.router.routes if isinstance(r, APIRoute)]
    read_names = {
        "vectorbt_kinds",
        "vectorbt_runs",
        "vectorbt_run_detail",
        "vectorbt_trades",
        "vectorbt_equity",
    }
    by_name = {r.name: (r.path, tuple(sorted(r.methods))) for r in routes if r.name in read_names}
    assert by_name == {
        "vectorbt_kinds": (f"{BASE}/kinds", ("GET",)),
        "vectorbt_runs": (f"{BASE}/runs", ("GET",)),
        "vectorbt_run_detail": (f"{BASE}/runs/{{run_id}}", ("GET",)),
        "vectorbt_trades": (f"{BASE}/runs/{{run_id}}/trades", ("GET",)),
        "vectorbt_equity": (f"{BASE}/runs/{{run_id}}/equity", ("GET",)),
    }


# ------------------------------------------------------------------------- GET /kinds
async def test_kinds_lists_builtins(service_settings: FinancesSettings) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/kinds")
    assert response.status_code == 200, response.text
    kinds = {info["kind"] for info in response.json()}
    assert set(BUILTIN_KINDS) <= kinds


async def test_kinds_filter_by_kind(service_settings: FinancesSettings) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/kinds", params={"kind": "sma_cross"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [info["kind"] for info in payload] == ["sma_cross"]


async def test_kinds_filter_unknown_is_empty(service_settings: FinancesSettings) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/kinds", params={"kind": "no_such_kind"})
    assert response.status_code == 200, response.text
    assert response.json() == []


# ------------------------------------------------------------------------- GET /runs
async def test_runs_lists_stored_runs(service_settings: FinancesSettings) -> None:
    sma_run = _make_run(sma_request(), service_settings)
    rsi_run = _make_run(_rsi_request(), service_settings)
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs")
    assert response.status_code == 200, response.text
    listed = {item["run_id"] for item in response.json()["items"]}
    assert {sma_run, rsi_run} <= listed


async def test_runs_filter_by_strategy_id(service_settings: FinancesSettings) -> None:
    request = sma_request()
    sma_run = _make_run(request, service_settings)
    _make_run(_rsi_request(), service_settings)
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs", params={"strategy_id": request.strategy_id})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["run_id"] for item in items] == [sma_run]
    assert all(item["strategy_id"] == request.strategy_id for item in items)


async def test_runs_cursor_pagination(service_settings: FinancesSettings) -> None:
    first_run = _make_run(sma_request(), service_settings)
    second_run = _make_run(_rsi_request(), service_settings)
    async with await _client() as client:
        first = await client.get(f"{BASE}/runs", params={"limit": 1})
        assert first.status_code == 200, first.text
        first_page = first.json()
        assert first_page["count"] == 1
        assert first_page["next_cursor"] is not None
        second = await client.get(
            f"{BASE}/runs", params={"limit": 1, "cursor": first_page["next_cursor"]}
        )
    assert second.status_code == 200, second.text
    second_page = second.json()
    first_ids = {item["run_id"] for item in first_page["items"]}
    second_ids = {item["run_id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {first_run, second_run}


@pytest.mark.parametrize("bad_limit", [0, 10001])
async def test_runs_limit_out_of_range_is_422(
    service_settings: FinancesSettings, bad_limit: int
) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs", params={"limit": bad_limit})
    assert response.status_code == 422, response.text


async def test_runs_bad_cursor_is_422(service_settings: FinancesSettings) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs", params={"cursor": "not-a-valid-cursor"})
    assert response.status_code == 422, response.text


# ------------------------------------------------------------------- GET /runs/{run_id}
async def test_run_detail_returns_manifest_and_summary(
    service_settings: FinancesSettings,
) -> None:
    run_id = _make_run(sma_request(), service_settings)
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs/{run_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["manifest"]["run_id"] == run_id
    assert body["summary"]["n_trades"] == FIXTURE_TRADES


async def test_run_detail_missing_run_is_404(service_settings: FinancesSettings) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs/{UNKNOWN_RUN}")
    assert response.status_code == 404, response.text


# ----------------------------------------------- GET /runs/{run_id}/trades y /equity
async def test_trades_paginate_with_after_ms(service_settings: FinancesSettings) -> None:
    run_id = _make_run(sma_request(), service_settings)
    async with await _client() as client:
        first = await client.get(f"{BASE}/runs/{run_id}/trades", params={"limit": 1})
        assert first.status_code == 200, first.text
        first_page = first.json()
        assert first_page["count"] == 1
        assert first_page["next_after_ms"] is not None
        cursor = first_page["next_after_ms"]
        second = await client.get(
            f"{BASE}/runs/{run_id}/trades", params={"limit": 1, "after_ms": cursor}
        )
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["entry_time_ms"] > cursor


async def test_equity_paginate_with_after_ms(service_settings: FinancesSettings) -> None:
    run_id = _make_run(sma_request(), service_settings)
    async with await _client() as client:
        first = await client.get(f"{BASE}/runs/{run_id}/equity", params={"limit": 1})
        assert first.status_code == 200, first.text
        first_page = first.json()
        assert first_page["count"] == 1
        assert first_page["next_after_ms"] is not None
        cursor = first_page["next_after_ms"]
        second = await client.get(
            f"{BASE}/runs/{run_id}/equity", params={"limit": 1, "after_ms": cursor}
        )
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["open_time_ms"] > cursor


@pytest.mark.parametrize("resource", ["trades", "equity"])
async def test_trades_and_equity_missing_run_is_404(
    service_settings: FinancesSettings, resource: str
) -> None:
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs/{UNKNOWN_RUN}/{resource}")
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("resource", ["trades", "equity"])
@pytest.mark.parametrize("bad_limit", [0, 10001])
async def test_trades_and_equity_limit_out_of_range_is_422(
    service_settings: FinancesSettings, resource: str, bad_limit: int
) -> None:
    run_id = _make_run(sma_request(), service_settings)
    async with await _client() as client:
        response = await client.get(f"{BASE}/runs/{run_id}/{resource}", params={"limit": bad_limit})
    assert response.status_code == 422, response.text
