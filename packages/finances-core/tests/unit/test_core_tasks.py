"""Connection lifecycle and concurrent initialisation of ``SQLTaskManager`` (Art. 2, Art. 16)."""

from __future__ import annotations

import gc
import sqlite3
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest

from finances_core import tasks
from finances_core.tasks import SQLTaskManager

THREADS = 8
ROUNDS = 5


def _exercise(manager: SQLTaskManager, task_id: str) -> None:
    manager.create_task(task_id)
    manager.update_task(task_id, status="RUNNING")
    manager.update_task(task_id, status="COMPLETED")
    manager.get_task(task_id)
    manager.list_tasks()
    manager.ensure_ready()


def test_operations_close_every_connection(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        manager = SQLTaskManager(tmp_path / "tasks.db")
        _exercise(manager, "t-1")
        del manager
        gc.collect()

    leaked = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert leaked == []


def test_failed_statement_rolls_back_and_closes(tmp_path: Path) -> None:
    manager = SQLTaskManager(tmp_path / "tasks.db")
    manager.create_task("t-1")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with pytest.raises(sqlite3.Error), manager._connect() as conn:
            conn.execute("UPDATE tasks SET status = 'RUNNING' WHERE task_id = 't-1'")
            conn.execute("SELECT * FROM missing_table")
        gc.collect()

    task = manager.get_task("t-1")
    assert task is not None
    assert task["status"] == "PENDING"
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


def test_database_is_in_wal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "tasks.db"
    SQLTaskManager(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


@pytest.mark.parametrize("round_", range(ROUNDS))
def test_concurrent_initialisation_of_a_new_database(tmp_path: Path, round_: int) -> None:
    db_path = tmp_path / f"round-{round_}" / "tasks.db"
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            _exercise(SQLTaskManager(db_path), f"t-{index}")
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rows = SQLTaskManager(db_path).list_tasks(limit=THREADS * 2)
    assert sorted(row["task_id"] for row in rows) == sorted(f"t-{i}" for i in range(THREADS))
    assert all(row["status"] == "COMPLETED" for row in rows)


def _locked_connect(calls: list[int]) -> Callable[[SQLTaskManager], AbstractContextManager[Any]]:
    @contextmanager
    def connect(_self: SQLTaskManager) -> Iterator[Any]:
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover - unreachable, keeps this a generator

    return connect


def test_initialisation_retries_lock_errors_within_the_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(tasks, "_BUSY_TIMEOUT_S", 0.3)
    monkeypatch.setattr(SQLTaskManager, "_connect", _locked_connect(calls))

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        SQLTaskManager(tmp_path / "tasks.db")

    assert len(calls) > 1
    # Generous margin: only proves the loop stops at the deadline, not timing precision.
    assert time.monotonic() - started < 0.3 + 10.0


def test_initialisation_raises_other_errors_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    @contextmanager
    def connect(_self: SQLTaskManager) -> Iterator[Any]:
        calls.append(1)
        raise sqlite3.OperationalError("disk I/O error")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(SQLTaskManager, "_connect", connect)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        SQLTaskManager(tmp_path / "tasks.db")
    assert calls == [1]
