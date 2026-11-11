"""G5: superficie de evaluación honesta — ``ft_vectorbt eval`` y ``GET /runs/{run_id}/eval``.

Verifica el comando CLI, la ruta API y el **contrato de dualidad** (ambos devuelven el mismo
``EvaluationReport`` para la misma corrida y defaults). Crea una corrida real con ``run_sync``
(con ``sl_frac``, para que el manifiesto persista el riesgo, G4) y la lee read-only.
"""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI
from finances_core.config import FinancesSettings
from typer.testing import CliRunner

from finances_backtester.ft_vectorbt import api, cli, service

from .conftest import sma_request

BASE = "/backtest/vectorbt"
UNKNOWN_RUN = "0" * 64
runner = CliRunner()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


def _make_run_with_stop(settings: FinancesSettings) -> str:
    return service.run_sync(sma_request(risk={"sl_frac": 0.1}), settings).run_id


def test_cli_eval_json(service_settings: FinancesSettings) -> None:
    run_id = _make_run_with_stop(service_settings)
    result = runner.invoke(cli.app, ["--format", "json", "eval", run_id])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["sl_frac"] == 0.1
    assert {"expectancy_r", "r_ci_low", "r_ci_high", "has_edge", "n_trades"} <= report.keys()


def test_cli_eval_not_found(service_settings: FinancesSettings) -> None:
    result = runner.invoke(cli.app, ["eval", UNKNOWN_RUN])
    assert result.exit_code != 0
    assert "no encontrada" in result.output


async def test_api_eval_endpoint(service_settings: FinancesSettings) -> None:
    run_id = _make_run_with_stop(service_settings)
    async with _client() as client:
        response = await client.get(f"{BASE}/runs/{run_id}/eval")
    assert response.status_code == 200
    report = response.json()
    assert report["sl_frac"] == 0.1
    assert "expectancy_r" in report


async def test_api_eval_unknown_is_404(service_settings: FinancesSettings) -> None:
    async with _client() as client:
        response = await client.get(f"{BASE}/runs/{UNKNOWN_RUN}/eval")
    assert response.status_code == 404


async def test_eval_cli_api_duality(service_settings: FinancesSettings) -> None:
    # Mismo run y defaults (seed 1234): CLI y API devuelven EXACTAMENTE el mismo reporte.
    run_id = _make_run_with_stop(service_settings)
    cli_result = runner.invoke(cli.app, ["--format", "json", "eval", run_id])
    assert cli_result.exit_code == 0, cli_result.output
    cli_report = json.loads(cli_result.output)
    async with _client() as client:
        api_report = (await client.get(f"{BASE}/runs/{run_id}/eval")).json()
    assert cli_report == api_report
