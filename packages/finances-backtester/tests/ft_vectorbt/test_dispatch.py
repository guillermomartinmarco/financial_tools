"""V2.2: ``service.dispatch`` + ``service.run_task`` (RF-8; Art. 3, 12, 16).

``dispatch`` valida y crea la tarea ``PENDING``; ``run_task`` ejecuta ``run_backtest`` en
un hilo (``asyncio.to_thread``) bajo el semáforo de ``FT_BACKTEST_MAX_CONCURRENT``. Las
fases y el estado terminal los escribe el ``runner`` del núcleo (B5.1); aquí se observan
en ``tasks.db`` y con un espía de ``SQLTaskManager.update_task``.

La espera con límite 1 es determinista: el primer ``simulate`` se bloquea en un
``threading.Event`` y se comprueba el **orden** de entrada/salida de VectorBT, no tiempos.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.backtest.engine_api import EngineOutput
from finances_core.backtest.errors import StrategyNotFoundError, UnknownKindError
from finances_core.config import FinancesSettings
from finances_core.marketdata.tasks_lifecycle import json_field
from finances_core.tasks import SQLTaskManager
from structlog.testing import capture_logs

from finances_backtester.ft_vectorbt import engine, service
from tests.contract.engine_contract import SYNTHETIC_DIR, SYNTHETIC_KIND
from tests.ft_vectorbt.conftest import (
    create_strategy,
    make_request,
    sma_request,
    task_row,
    tree_digest,
)

FULL_PHASES = ["LOADING", "SIGNALS", "SIMULATING", "PERSISTING", "COMPLETED"]
ORDER = ["PENDING", *FULL_PHASES]
WAIT_S = 60.0  # holgura para runners de CI lentos; nunca se espera de más si todo va bien


@pytest.fixture
def statuses(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Espía de ``SQLTaskManager.update_task``: ``(task_id, status)`` en orden de escritura."""
    original = SQLTaskManager.update_task
    seen: list[tuple[str, str]] = []
    lock = threading.Lock()

    def spy(self: SQLTaskManager, task_id: str, **fields: Any) -> None:
        if "status" in fields:
            with lock:
                seen.append((task_id, str(fields["status"])))
        original(self, task_id, **fields)

    monkeypatch.setattr(SQLTaskManager, "update_task", spy)
    return seen


def _of(statuses: list[tuple[str, str]], task_id: str) -> list[str]:
    return [status for tid, status in statuses if tid == task_id]


def _assert_failed(settings: FinancesSettings, task_id: str, code: str) -> None:
    row = task_row(settings, task_id)
    assert (row["status"], row["phase"]) == ("FAILED", "FAILED")
    assert str(row["error"]).startswith(f"{code}: ")
    assert json_field(row, "errors_json", []) == [row["error"]]
    assert row["finished_at_ms"] is not None
    assert tree_digest(settings.RESULTS_DIR) == {}  # nada escrito


# ------------------------------------------------------------------ éxito
async def test_dispatch_creates_pending_task_and_run_task_completes(
    service_settings: FinancesSettings, statuses: list[tuple[str, str]]
) -> None:
    dispatched = service.dispatch(sma_request(), service_settings)

    assert uuid.UUID(dispatched.task_id).version == 7
    row = task_row(service_settings, dispatched.task_id)
    assert (row["status"], row["phase"]) == ("PENDING", "PENDING")
    assert row["started_at_ms"] is None
    assert tree_digest(service_settings.RESULTS_DIR) == {}  # despachar no calcula

    result = await service.run_task(dispatched, service_settings)

    assert result is not None
    assert result.cached is False
    row = task_row(service_settings, dispatched.task_id)
    assert (row["status"], row["phase"]) == ("COMPLETED", "COMPLETED")
    assert row["error"] is None
    assert json_field(row, "result_summary_json", {}) == {
        "run_id": result.run_id,
        "cached": False,
        "n_bars": 2400,
        "n_trades": result.summary.n_trades,
    }
    assert result.summary.n_trades > 0
    assert _of(statuses, dispatched.task_id) == FULL_PHASES


async def test_phases_never_go_back_and_cached_rerun_skips_to_completed(
    service_settings: FinancesSettings, statuses: list[tuple[str, str]]
) -> None:
    request = sma_request()
    first = service.dispatch(request, service_settings)
    await service.run_task(first, service_settings)
    second = service.dispatch(request, service_settings)

    result = await service.run_task(second, service_settings)

    assert result is not None
    assert result.cached is True
    for task_id, expected in ((first.task_id, FULL_PHASES), (second.task_id, ["LOADING"])):
        seen = _of(statuses, task_id)
        indexes = [ORDER.index(s) for s in seen]
        assert indexes == sorted(set(indexes)), seen  # estrictamente hacia delante
        assert seen[: len(expected)] == expected
        assert seen[-1] == "COMPLETED"
    summary = json_field(task_row(service_settings, second.task_id), "result_summary_json", {})
    assert summary["cached"] is True
    assert summary["run_id"] == result.run_id


async def test_each_dispatch_gets_a_fresh_task_and_terminal_tasks_keep_their_state(
    service_settings: FinancesSettings, statuses: list[tuple[str, str]]
) -> None:
    """Un ``task_id`` terminal nunca se reutiliza: cada despacho genera un UUID v7 nuevo."""
    request = sma_request()
    first = service.dispatch(request, service_settings)
    await service.run_task(first, service_settings)
    finished = task_row(service_settings, first.task_id)

    second = service.dispatch(request, service_settings)
    await service.run_task(second, service_settings)

    assert second.task_id != first.task_id
    assert task_row(service_settings, first.task_id) == finished
    assert _of(statuses, first.task_id) == FULL_PHASES  # sin fases nuevas


# ---------------------------------------------------------------- validación
def test_unknown_strategy_is_rejected_before_creating_a_task(
    service_settings: FinancesSettings,
) -> None:
    request = make_request("01890a5d-ac96-774b-bcce-b302099a8001")  # UUID v7 inexistente

    with pytest.raises(StrategyNotFoundError):
        service.dispatch(request, service_settings)

    assert SQLTaskManager(service_settings.TASKS_DB_PATH).list_tasks() == []


def test_invalid_parameters_are_rejected_before_creating_a_task(
    service_settings: FinancesSettings,
) -> None:
    request = make_request(create_strategy({"kind": "no_such_kind", "params": {}}))

    with pytest.raises(UnknownKindError):
        service.dispatch(request, service_settings)

    assert SQLTaskManager(service_settings.TASKS_DB_PATH).list_tasks() == []


# ------------------------------------------------------------------- fallos
async def test_vectorbt_exception_fails_the_task(
    service_settings: FinancesSettings,
    statuses: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("vectorbt exploded")

    monkeypatch.setattr(engine, "simulate", boom)
    dispatched = service.dispatch(sma_request(), service_settings)

    result = await service.run_task(dispatched, service_settings)

    assert result is None
    _assert_failed(service_settings, dispatched.task_id, "RuntimeError")
    assert "vectorbt exploded" in task_row(service_settings, dispatched.task_id)["error"]
    assert _of(statuses, dispatched.task_id) == ["LOADING", "SIGNALS", "SIMULATING", "FAILED"]


async def test_output_out_of_range_fails_the_task(
    service_settings: FinancesSettings,
    statuses: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = engine.from_framework_output

    def corrupt(*args: Any, **kwargs: Any) -> EngineOutput:
        out = original(*args, **kwargs)
        return EngineOutput(
            trades=out.trades, equity=out.equity.with_columns(pl.col("equity").cast(pl.Int64))
        )

    monkeypatch.setattr(engine, "from_framework_output", corrupt)
    dispatched = service.dispatch(sma_request(), service_settings)

    assert await service.run_task(dispatched, service_settings) is None

    _assert_failed(service_settings, dispatched.task_id, "output_out_of_range")
    assert _of(statuses, dispatched.task_id) == ["LOADING", "SIGNALS", "SIMULATING", "FAILED"]


async def test_exception_in_local_strategy_signals_fails_the_task(
    service_settings: FinancesSettings,
    statuses: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = Path(service_settings.STRATEGIES_DIR) / SYNTHETIC_KIND
    shutil.copytree(SYNTHETIC_DIR / SYNTHETIC_KIND, folder, ignore=shutil.ignore_patterns("__*"))
    logic = folder / "logic.py"
    body = (
        "        t = pl.int_range(pl.len(), dtype=pl.Int64)\n"
        "        return candles.select(\n"
        "            position_from_events(\n"
        "                ((t * 7) % params.entry_stride) == 0,\n"
        "                ((t * 5 + 1) % params.exit_stride) == 0,\n"
        "            )\n"
        "        ).to_series()"
    )
    text = logic.read_text(encoding="utf-8")
    assert body in text
    logic.write_text(text.replace(body, '        raise ValueError("local boom")'), encoding="utf-8")
    simulate_calls: list[int] = []
    monkeypatch.setattr(engine, "simulate", lambda *a, **k: simulate_calls.append(1))
    request = make_request(create_strategy({"kind": SYNTHETIC_KIND, "params": {}}))
    dispatched = service.dispatch(request, service_settings)

    assert await service.run_task(dispatched, service_settings) is None

    _assert_failed(service_settings, dispatched.task_id, "strategy_runtime_error")
    assert simulate_calls == []
    assert _of(statuses, dispatched.task_id) == ["LOADING", "SIGNALS", "FAILED"]


async def test_failure_before_the_runner_fails_the_task(
    service_settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_engine() -> Any:
        raise RuntimeError("engine init")

    monkeypatch.setattr(service, "VectorbtEngine", broken_engine)
    dispatched = service.dispatch(sma_request(), service_settings)

    with capture_logs() as logs:
        result = await service.run_task(dispatched, service_settings)

    assert result is None
    _assert_failed(service_settings, dispatched.task_id, "RuntimeError")
    assert "engine init" in task_row(service_settings, dispatched.task_id)["error"]
    assert [e["event"] for e in logs if e["event"] == "backtest_task_failed"] == [
        "backtest_task_failed"
    ]


async def test_runner_failure_is_not_logged_twice(
    service_settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("vectorbt exploded")

    monkeypatch.setattr(engine, "simulate", boom)
    dispatched = service.dispatch(sma_request(), service_settings)

    with capture_logs() as logs:
        assert await service.run_task(dispatched, service_settings) is None

    assert not [e for e in logs if e["event"] == "backtest_task_failed"]


# -------------------------------------------------------------- concurrencia
class _Gate:
    """``simulate`` instrumentado: registra entradas/salidas y puede bloquear la primera."""

    def __init__(self, *, block_first: bool, barrier: threading.Barrier | None = None) -> None:
        self.events: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_first = block_first
        self.barrier = barrier
        self._lock = threading.Lock()
        self._original = engine.simulate

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            index = sum(e.startswith("enter") for e in self.events)
            self.events.append(f"enter{index}")
        if self.barrier is not None:
            self.barrier.wait()  # solo pasa si dos simulaciones coinciden en el tiempo
        if index == 0 and self.block_first:
            self.entered.set()
            assert self.release.wait(WAIT_S), "la primera corrida nunca fue liberada"
        try:
            return self._original(*args, **kwargs)
        finally:
            with self._lock:
                self.events.append(f"exit{index}")


def _two_requests() -> tuple[Any, Any]:
    first = sma_request()
    other = create_strategy({"kind": "sma_cross", "params": {"fast": 3, "slow": 12}})
    return first, make_request(other)  # run_id distintos: ambas simulan


async def test_with_limit_one_the_second_run_waits(
    monkeypatch: pytest.MonkeyPatch, service_settings: FinancesSettings
) -> None:
    monkeypatch.setenv("FT_BACKTEST_MAX_CONCURRENT", "1")
    settings = FinancesSettings()
    assert settings.BACKTEST_MAX_CONCURRENT == 1
    gate = _Gate(block_first=True)
    monkeypatch.setattr(engine, "simulate", gate)
    req_a, req_b = _two_requests()
    a = service.dispatch(req_a, settings)
    b = service.dispatch(req_b, settings)

    run_a = asyncio.create_task(service.run_task(a, settings))
    assert await asyncio.to_thread(gate.entered.wait, WAIT_S)
    run_b = asyncio.create_task(service.run_task(b, settings))
    # Oportunidad amplia para que b arranque si el semáforo no la retuviera; la
    # aserción solo puede fallar si b entra (sin falsos negativos por lentitud).
    await asyncio.sleep(0.5)
    assert gate.events == ["enter0"]
    assert task_row(settings, a.task_id)["status"] == "SIMULATING"
    assert task_row(settings, b.task_id)["status"] == "PENDING"

    gate.release.set()
    results = await asyncio.wait_for(asyncio.gather(run_a, run_b), timeout=WAIT_S * 2)

    assert all(r is not None and not r.cached for r in results)
    assert gate.events == ["enter0", "exit0", "enter1", "exit1"]  # en serie
    for task in (a, b):
        assert task_row(settings, task.task_id)["status"] == "COMPLETED"


async def test_cancelled_while_waiting_fails_the_task(
    monkeypatch: pytest.MonkeyPatch, service_settings: FinancesSettings
) -> None:
    monkeypatch.setenv("FT_BACKTEST_MAX_CONCURRENT", "1")
    settings = FinancesSettings()
    gate = _Gate(block_first=True)
    monkeypatch.setattr(engine, "simulate", gate)
    req_a, req_b = _two_requests()
    a = service.dispatch(req_a, settings)
    b = service.dispatch(req_b, settings)

    run_a = asyncio.create_task(service.run_task(a, settings))
    assert await asyncio.to_thread(gate.entered.wait, WAIT_S)
    run_b = asyncio.create_task(service.run_task(b, settings))
    await asyncio.sleep(0)  # b llega al semáforo y espera
    run_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_b

    row = task_row(settings, b.task_id)
    assert row["status"] == "FAILED"
    assert row["error"].startswith("cancelled")
    gate.release.set()
    assert await asyncio.wait_for(run_a, timeout=WAIT_S) is not None
    assert gate.events == ["enter0", "exit0"]  # b nunca simuló


async def test_with_limit_two_both_runs_simulate_at_once(
    monkeypatch: pytest.MonkeyPatch, service_settings: FinancesSettings
) -> None:
    """Control del test anterior: con límite 2 las dos simulaciones coinciden (barrera)."""
    monkeypatch.setenv("FT_BACKTEST_MAX_CONCURRENT", "2")
    settings = FinancesSettings()
    gate = _Gate(block_first=False, barrier=threading.Barrier(2, timeout=WAIT_S))
    monkeypatch.setattr(engine, "simulate", gate)
    req_a, req_b = _two_requests()
    a = service.dispatch(req_a, settings)
    b = service.dispatch(req_b, settings)

    results = await asyncio.wait_for(
        asyncio.gather(service.run_task(a, settings), service.run_task(b, settings)),
        timeout=WAIT_S * 2,
    )

    assert all(r is not None for r in results)
    assert sorted(gate.events[:2]) == ["enter0", "enter1"]


async def test_concurrent_dispatches_of_same_request_leave_one_result(
    service_settings: FinancesSettings,
) -> None:
    request = sma_request()
    tasks = [service.dispatch(request, service_settings) for _ in range(4)]

    results = await asyncio.wait_for(
        asyncio.gather(*(service.run_task(t, service_settings) for t in tasks)),
        timeout=WAIT_S * 2,
    )

    assert all(r is not None for r in results)
    assert len({r.run_id for r in results if r is not None}) == 1
    assert len(list(service_settings.RESULTS_DIR.rglob("manifest.json"))) == 1
    for task in tasks:
        assert task_row(service_settings, task.task_id)["status"] == "COMPLETED"
