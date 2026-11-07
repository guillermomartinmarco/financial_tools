"""V4.3: contrato CLI↔API (Art. 1, RF-7) — una misma petición por ambos adaptadores.

Prueba la *dualidad obligatoria* del Art. 1: la CLI (``ft_vectorbt run``/``trades``/``equity``
con ``--format json``) y la API (``POST /backtest/vectorbt/run`` → *polling* → ``GET /result``;
``GET /runs/{run_id}/trades`` y ``/equity``) comparten la **misma lógica de servicio**
(``service.run_sync``/``run_backtest`` y los lectores de página) y producen salidas equivalentes
para la misma :class:`BacktestRequest`. El test solo orquesta los dos adaptadores reales y
compara; no reimplementa nada de la lógica de producción (Art. 1, RF-7 último bullet).

**Enfoque de paridad: RESULTS_DIR compartido (determinista).** Ambas rutas corren contra el
mismo ``FT_PROJECT_ROOT``/``RESULTS_DIR`` temporal (la *fixture* ``service_settings`` fija las
variables ``FT_*`` que tanto la CLI como la API leen al construir ``FinancesSettings()``). La
CLI corre primero y **calcula** (``cached=false``); la API corre después con un cuerpo idéntico
y, al compartir el mismo almacén y, por tanto, el mismo ``run_id``, **reutiliza** el resultado
(``cached=true``). Esto demuestra directamente el Art. 1: el segundo adaptador ve el resultado
del primero sin recalcular. Se compara todo el :class:`BacktestTaskResult` salvo ``cached``.

Para construir *la misma* petición, la CLI recibe por *flags* exactamente los campos de la
:class:`BacktestRequest` de ``sma_request()`` (``--dataset``, ``--strategy-id``, ``--fees-bps``,
``--slippage-bps``; el resto son los mismos valores por defecto del esquema), de modo que ambos
adaptadores validan una petición idéntica y, por ende, el mismo ``run_id``.

Nota TDD (RED/GREEN): al ser un contrato de paridad sobre código ya existente (V3.2 + V4.2), las
aserciones de igualdad pasan en la primera ejecución (GREEN) — es el resultado esperado de un
test de contrato (no hay defecto que reparar). Se verificó que el test **detecta** una ruptura
de paridad: con cuerpos distintos (otro ``fees``) los ``run_id`` divergen y la aserción falla
(RED observado durante el desarrollo; ver el handoff). Sin red; estado solo bajo ``tmp_path``.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import (
    BacktestRequest,
    BacktestTaskResult,
    EquityPage,
    TradesPage,
)
from typer.testing import CliRunner

from finances_backtester.ft_vectorbt import api, cli

from .conftest import DATASET, sma_request

runner = CliRunner()

BASE = "/backtest/vectorbt"
#: Límite amplio para traer ``trades`` (55) y ``equity`` (2 400) en una sola página en ambos lados.
PAGE_LIMIT = 10000


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


async def _poll(client: httpx.AsyncClient, task_id: str, *, tries: int = 200) -> dict[str, Any]:
    status: dict[str, Any] = {}
    for _ in range(tries):
        status = (await client.get(f"{BASE}/status/{task_id}")).json()
        if status["status"] in {"COMPLETED", "FAILED"}:
            break
    return status


def _cli_run(request: BacktestRequest) -> BacktestTaskResult:
    """Ejecuta ``ft_vectorbt run`` (misma petición, por *flags*) y parsea el JSON de *stdout*."""
    result = runner.invoke(
        cli.app,
        [
            "--format",
            "json",
            "run",
            "-d",
            DATASET.path_segment(),
            "--strategy-id",
            request.strategy_id,
            "--fees-bps",
            str(request.fees_bps),
            "--slippage-bps",
            str(request.slippage_bps),
        ],
    )
    assert result.exit_code == 0, result.output
    return BacktestTaskResult.model_validate_json(result.stdout)


async def _api_run(client: httpx.AsyncClient, request: BacktestRequest) -> BacktestTaskResult:
    """Despacha ``POST /run``, hace *polling* hasta ``COMPLETED`` y lee ``GET /result``."""
    response = await client.post(f"{BASE}/run", json=request.model_dump(mode="json"))
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    status = await _poll(client, task_id)
    assert status["status"] == "COMPLETED", status
    result = await client.get(f"{BASE}/result/{task_id}")
    assert result.status_code == 200, result.text
    return BacktestTaskResult.model_validate_json(result.content)


# ----------------------------------------------- contrato del resumen y el manifiesto
async def test_cli_and_api_agree_on_summary_and_manifest(
    service_settings: FinancesSettings,
) -> None:
    """La CLI calcula y la API reutiliza (``cached``): mismo ``run_id``, resumen y manifiesto."""
    request = sma_request()

    cli_result = _cli_run(request)  # calcula primero (cached=false)
    async with await _client() as client:
        api_result = await _api_run(client, request)  # reutiliza (cached=true)

    # Misma petición → mismo run_id por el almacén compartido (Art. 1).
    assert cli_result.run_id == api_result.run_id
    # El primero calculó, el segundo reutilizó el resultado del primero.
    assert cli_result.cached is False
    assert api_result.cached is True
    # Resumen idéntico (todas las métricas) y manifiesto idéntico
    # (engine/engine_version/identidad de estrategia/costes).
    assert cli_result.summary == api_result.summary
    assert cli_result.manifest == api_result.manifest


# -------------------------------------------------------- contrato de trades y equity
async def test_cli_and_api_agree_on_trades(service_settings: FinancesSettings) -> None:
    request = sma_request()
    cli_result = _cli_run(request)
    run_id = cli_result.run_id

    cli_invoke = runner.invoke(
        cli.app, ["--format", "json", "trades", run_id, "--limit", str(PAGE_LIMIT)]
    )
    assert cli_invoke.exit_code == 0, cli_invoke.output
    cli_page = TradesPage.model_validate_json(cli_invoke.stdout)

    async with await _client() as client:
        response = await client.get(f"{BASE}/runs/{run_id}/trades", params={"limit": PAGE_LIMIT})
    assert response.status_code == 200, response.text
    api_page = TradesPage.model_validate_json(response.content)

    assert cli_page.items == api_page.items
    assert cli_page.count == api_page.count
    assert cli_page.next_after_ms == api_page.next_after_ms


async def test_cli_and_api_agree_on_equity(service_settings: FinancesSettings) -> None:
    request = sma_request()
    cli_result = _cli_run(request)
    run_id = cli_result.run_id

    cli_invoke = runner.invoke(
        cli.app, ["--format", "json", "equity", run_id, "--limit", str(PAGE_LIMIT)]
    )
    assert cli_invoke.exit_code == 0, cli_invoke.output
    cli_page = EquityPage.model_validate_json(cli_invoke.stdout)

    async with await _client() as client:
        response = await client.get(f"{BASE}/runs/{run_id}/equity", params={"limit": PAGE_LIMIT})
    assert response.status_code == 200, response.text
    api_page = EquityPage.model_validate_json(response.content)

    assert cli_page.items == api_page.items
    assert cli_page.count == api_page.count
    assert cli_page.next_after_ms == api_page.next_after_ms
