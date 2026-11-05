"""V2.3: logging y auditoría de ``ft_vectorbt`` (RF-10; ``finances_backtester`` RF-13).

El evento ``backtest_run`` lo emite una sola vez el ``runner`` del núcleo; el servicio
solo añade el contexto (``structlog.contextvars``) de la corrida: ``task_id``,
``dataset``, ``symbol`` y ``timeframe``. Se captura con ``merge_contextvars`` para ver
el contexto igual que en producción (``finances_core.logging``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from finances_core.config import FinancesSettings
from structlog.testing import capture_logs

from finances_backtester.ft_vectorbt import engine, service
from tests.ft_vectorbt.conftest import DATASET, sma_request

#: Campos de ``finances_backtester`` RF-13 (los mismos que fija ``test_backtest_runner``).
AUDIT_FIELDS = {
    "task_id",
    "run_id",
    "engine",
    "engine_version",
    "strategy_id",
    "strategy_version",
    "strategy_hash",
    "kind",
    "kind_version",
    "strategy_code_hash",
    "strategy_origin",
    "dataset",
    "symbol",
    "timeframe",
    "start_ms",
    "end_ms",
    "n_bars",
    "n_trades",
    "cached",
    "forced",
    "duration_ms",
    "result",
    "load_ms",
    "signals_ms",
    "engine_ms",
    "persist_ms",
}


@pytest.fixture
def logs() -> Iterator[list[dict[str, Any]]]:
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as captured:
        yield captured


def _audit(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "backtest_run"]


def _assert_common(event: dict[str, Any], task_id: str, result: str) -> None:
    assert set(event) >= AUDIT_FIELDS, AUDIT_FIELDS - set(event)
    assert event["result"] == result
    assert event["engine"] == "vectorbt"
    assert str(event["engine_version"]).startswith("vectorbt ")
    assert event["task_id"] == task_id
    assert event["dataset"] == DATASET.path_segment()
    assert (event["symbol"], event["timeframe"]) == (DATASET.symbol, DATASET.timeframe)
    assert isinstance(event["run_id"], str)
    assert event["run_id"]
    assert (event["kind"], event["strategy_origin"]) == ("sma_cross", "builtin")
    assert event["kind_version"]
    assert event["strategy_code_hash"]
    assert event["n_bars"] == 2400


async def test_completed_and_cached_runs_emit_one_audit_event_each(
    service_settings: FinancesSettings, logs: list[dict[str, Any]]
) -> None:
    request = sma_request()
    first = service.dispatch(request, service_settings)
    result = await service.run_task(first, service_settings)
    assert result is not None

    (completed,) = _audit(logs)
    _assert_common(completed, first.task_id, "completed")
    assert completed["run_id"] == result.run_id
    assert completed["cached"] is False
    assert completed["n_trades"] == result.summary.n_trades
    for metric in ("load_ms", "signals_ms", "engine_ms", "persist_ms"):
        assert isinstance(completed[metric], float), metric
    # task_phase no recibe dataset como argumento: llega por el contexto del servicio.
    phases = [e for e in logs if e["event"] == "task_phase"]
    assert [e["phase"] for e in phases] == [
        "LOADING",
        "SIGNALS",
        "SIMULATING",
        "PERSISTING",
        "COMPLETED",
    ]
    assert all(e["dataset"] == DATASET.path_segment() for e in phases)
    assert all(e["task_id"] == first.task_id for e in phases)

    second = service.dispatch(request, service_settings)
    await service.run_task(second, service_settings)

    cached = _audit(logs)[1]
    assert len(_audit(logs)) == 2
    _assert_common(cached, second.task_id, "cached")
    assert cached["run_id"] == result.run_id
    assert cached["cached"] is True
    assert cached["signals_ms"] is None  # la caché no calcula señales
    assert cached["engine_ms"] is None


async def test_failed_run_emits_audit_event_with_context(
    service_settings: FinancesSettings,
    logs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("vectorbt exploded")

    monkeypatch.setattr(engine, "simulate", boom)
    dispatched = service.dispatch(sma_request(), service_settings)

    assert await service.run_task(dispatched, service_settings) is None

    (failed,) = _audit(logs)
    _assert_common(failed, dispatched.task_id, "failed")
    assert failed["error"] == "RuntimeError: vectorbt exploded"
    assert isinstance(failed["signals_ms"], float)
    assert failed["engine_ms"] is None
    # El contexto llega también a los demás logs de la corrida (p. ej. el error).
    (error,) = [e for e in logs if e["event"] == "backtest_run_failed"]
    assert error["task_id"] == dispatched.task_id
    assert error["dataset"] == DATASET.path_segment()


def test_context_is_bound_only_during_the_run(
    service_settings: FinancesSettings, logs: list[dict[str, Any]]
) -> None:
    service.run_sync(sma_request(), service_settings)
    structlog.get_logger("after").info("after_run")

    (event,) = _audit(logs)  # corrida síncrona (CLI): sin task_id
    assert event["task_id"] is None
    assert event["engine"] == "vectorbt"
    assert event["result"] == "completed"
    (after,) = [e for e in logs if e["event"] == "after_run"]
    assert "dataset" not in after
    assert "task_id" not in after
