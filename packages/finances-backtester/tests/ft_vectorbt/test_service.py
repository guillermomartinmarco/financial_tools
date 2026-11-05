"""V2.1: ``service.run_sync`` sobre ``finances_core.backtest.runner.run_backtest`` (RF-4, RF-5).

Almacén (``FT_RESULTS_DIR``), ``tasks.db`` y ``strategies.db`` temporales bajo
``FT_PROJECT_ROOT = tmp_path``; la fuente es el *fixture* de la suite de contrato. El
espía envuelve ``engine.simulate``, la única llamada a VectorBT de ``VectorbtEngine.run``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from finances_core.backtest import results_store
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import BacktestTaskResult

from finances_backtester.ft_vectorbt import engine, service
from tests.ft_vectorbt.conftest import DATASET, sma_request, tree_digest

RESULT_FILES = ("trades.parquet", "equity.parquet", "summary.json")
FIXTURE_TRADES = 55  # sma_cross con los parámetros de los golden (B6.1)


@pytest.fixture
def simulate_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Espía de ``engine.simulate``: cuántas veces se invoca a VectorBT."""
    original = engine.simulate
    calls: list[int] = []
    lock = threading.Lock()

    def spy(*args: Any, **kwargs: Any) -> Any:
        with lock:
            calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "simulate", spy)
    return calls


def _run_dir(settings: FinancesSettings, result: BacktestTaskResult) -> Path:
    return results_store.run_dir(settings.RESULTS_DIR, DATASET, result.run_id)


def _result_bytes(settings: FinancesSettings, result: BacktestTaskResult) -> dict[str, bytes]:
    folder = _run_dir(settings, result)
    return {name: (folder / name).read_bytes() for name in RESULT_FILES}


def test_run_sync_writes_complete_result(
    service_settings: FinancesSettings, simulate_calls: list[int]
) -> None:
    seen: list[str] = []

    result = service.run_sync(sma_request(), service_settings, seen.append)

    assert result.cached is False
    assert simulate_calls == [1]
    assert seen == ["LOADING", "SIGNALS", "SIMULATING", "PERSISTING", "COMPLETED"]
    assert result.manifest.engine == "vectorbt"
    assert result.manifest.engine_version.startswith("vectorbt ")
    assert result.summary.n_bars == 2400
    assert result.summary.n_trades == FIXTURE_TRADES
    folder = _run_dir(service_settings, result)
    for name in (*RESULT_FILES, "manifest.json"):
        assert (folder / name).is_file(), name
    stored = results_store.find_run(service_settings.RESULTS_DIR, result.run_id)
    assert stored is not None
    assert stored.manifest == result.manifest
    assert stored.summary == result.summary


def test_repeat_is_cached_without_invoking_vectorbt(
    service_settings: FinancesSettings, simulate_calls: list[int]
) -> None:
    request = sma_request()
    first = service.run_sync(request, service_settings)
    before = tree_digest(service_settings.RESULTS_DIR)
    seen: list[str] = []

    again = service.run_sync(request, service_settings, seen.append)

    assert again.cached is True
    assert simulate_calls == [1]  # solo la primera corrida simuló
    assert seen == ["LOADING", "COMPLETED"]
    assert again.run_id == first.run_id
    assert again.summary == first.summary
    assert tree_digest(service_settings.RESULTS_DIR) == before


def test_force_recomputes_with_byte_identical_files(
    service_settings: FinancesSettings, simulate_calls: list[int]
) -> None:
    request = sma_request()
    first = service.run_sync(request, service_settings)
    first_bytes = _result_bytes(service_settings, first)

    forced = service.run_sync(request.model_copy(update={"force": True}), service_settings)

    assert forced.cached is False
    assert simulate_calls == [1, 1]
    assert forced.run_id == first.run_id  # force no forma parte de la identidad
    assert _result_bytes(service_settings, forced) == first_bytes


def _run_concurrently(n: int, target: Callable[[], BacktestTaskResult]) -> list[Any]:
    barrier = threading.Barrier(n, timeout=60)
    outcomes: list[Any] = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        try:
            outcomes[i] = target()
        except Exception as exc:  # noqa: BLE001 - se comprueba abajo
            outcomes[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
        assert not thread.is_alive()
    return outcomes


@pytest.mark.parametrize("force", [False, True])
def test_concurrent_same_run_id_leaves_a_single_result(
    service_settings: FinancesSettings, force: bool
) -> None:
    request = sma_request(force=force)

    outcomes = _run_concurrently(4, lambda: service.run_sync(request, service_settings))

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert errors == []
    run_ids = {o.run_id for o in outcomes}
    assert len(run_ids) == 1
    (run_id,) = run_ids
    manifests = sorted(service_settings.RESULTS_DIR.rglob("manifest.json"))
    assert len(manifests) == 1
    assert manifests[0].parent == results_store.run_dir(
        service_settings.RESULTS_DIR, DATASET, run_id
    )
    # Sin restos de escrituras temporales o apartadas junto al resultado instalado.
    assert sorted(p.name for p in manifests[0].parent.parent.iterdir()) == [run_id]
    stored = results_store.find_run(service_settings.RESULTS_DIR, run_id)
    assert stored is not None
    assert all(o.summary == stored.summary for o in outcomes)


def test_nothing_is_written_under_parquet_dir(service_settings: FinancesSettings) -> None:
    before = tree_digest(service_settings.PARQUET_DIR)
    assert before  # la fuente existe
    request = sma_request()

    service.run_sync(request, service_settings)
    service.run_sync(request, service_settings)
    service.run_sync(request.model_copy(update={"force": True}), service_settings)

    assert tree_digest(service_settings.PARQUET_DIR) == before


def test_run_sync_defaults_to_environment_settings(
    service_settings: FinancesSettings,
) -> None:
    result = service.run_sync(sma_request())

    assert results_store.find_run(service_settings.RESULTS_DIR, result.run_id) is not None
