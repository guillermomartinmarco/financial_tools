"""T1.3: ciclo de vida de tareas del núcleo (fases monótonas, despacho, consultas)."""

from __future__ import annotations

import os

import pytest
from structlog.testing import capture_logs

from finances_core.config import FinancesSettings
from finances_core.marketdata.tasks_lifecycle import (
    MAX_CONCURRENT_ENV,
    TaskNotFinishedError,
    TaskRecorder,
    create_download_task,
    ensure_finished,
    json_field,
    load_task_row,
    resolve_max_concurrent,
    run_download_sync,
)

YF_PHASES = ("PENDING", "PLANNING", "DOWNLOADING", "NORMALIZING", "WRITING_PARQUET")


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


def test_create_download_task_is_pending_and_idempotent(settings: FinancesSettings) -> None:
    create_download_task("t1", {"pairs_total": [["AAPL", "1d"]]}, settings=settings)
    create_download_task("t1", {"pairs_total": []}, settings=settings)
    row = load_task_row("t1", settings)
    assert row is not None
    assert row["status"] == "PENDING"
    assert json_field(row, "result_summary_json", {}) == {"pairs_total": [["AAPL", "1d"]]}
    assert json_field(row, "errors_json", []) == []


def test_load_missing_row_returns_none(settings: FinancesSettings) -> None:
    assert load_task_row("missing", settings) is None


async def test_phases_only_advance(settings: FinancesSettings) -> None:
    seen: list[str] = []
    rec = await TaskRecorder.open("t2", settings, seen.append, phase_order=YF_PHASES)
    await rec.phase("DOWNLOADING")
    await rec.phase("PLANNING")  # retroceso ignorado
    await rec.phase("DOWNLOADING")  # repetición ignorada
    await rec.phase("WRITING_PARQUET")
    await rec.phase("COMPLETED")
    assert seen == ["DOWNLOADING", "WRITING_PARQUET", "COMPLETED"]
    row = load_task_row("t2", settings)
    assert row is not None
    assert row["status"] == "COMPLETED"


async def test_unknown_phase_is_rejected(settings: FinancesSettings) -> None:
    rec = await TaskRecorder.open("t3", settings, None, phase_order=YF_PHASES)
    with pytest.raises(ValueError, match="DOWNLOADING_PUBLIC"):
        await rec.phase("DOWNLOADING_PUBLIC")


async def test_reopen_resumes_phase_summary_and_errors(settings: FinancesSettings) -> None:
    create_download_task("t4", {"pairs_total": [["A", "1d"]]}, settings=settings)
    rec = await TaskRecorder.open("t4", settings, None, phase_order=YF_PHASES)
    await rec.phase("NORMALIZING")
    await rec.add_error("boom")
    rec.mark_pairs_done([["A", "1d"], ["A", "1d"]])
    await rec.save_summary()

    again = await TaskRecorder.open("t4", settings, None, phase_order=YF_PHASES)
    assert again.errors == ["boom"]
    assert again.summary["pairs_done"] == [["A", "1d"]]
    await again.phase("PLANNING")
    row = load_task_row("t4", settings)
    assert row is not None
    assert row["status"] == "NORMALIZING"


async def test_mark_failed_records_error(settings: FinancesSettings) -> None:
    seen: list[str] = []
    rec = await TaskRecorder.open("t5", settings, seen.append, phase_order=YF_PHASES)
    await rec.mark_failed("rate limit agotado")
    row = load_task_row("t5", settings)
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["error"] == "rate limit agotado"
    assert json_field(row, "errors_json", []) == ["rate limit agotado"]
    assert seen == ["FAILED"]
    await rec.phase("COMPLETED")  # tras FAILED no se avanza
    assert seen == ["FAILED"]


def test_ensure_finished(settings: FinancesSettings) -> None:
    create_download_task("t6", {}, settings=settings)
    row = load_task_row("t6", settings)
    assert row is not None
    with pytest.raises(TaskNotFinishedError) as info:
        ensure_finished("t6", row)
    assert info.value.status == "PENDING"
    assert ensure_finished("t6", {**row, "status": "COMPLETED"}) == "COMPLETED"


def test_resolve_max_concurrent(
    monkeypatch: pytest.MonkeyPatch, settings: FinancesSettings
) -> None:
    monkeypatch.delenv(MAX_CONCURRENT_ENV, raising=False)
    assert resolve_max_concurrent(3, settings) == 3
    with capture_logs() as logs:
        assert resolve_max_concurrent(None, settings) == settings.DOWNLOAD_MAX_CONCURRENT
    assert os.environ[MAX_CONCURRENT_ENV] == str(settings.DOWNLOAD_MAX_CONCURRENT)
    assert any(e["event"].startswith("Usando default") for e in logs)


def test_run_download_sync_never_raises() -> None:
    calls: list[str] = []

    async def ok() -> None:
        calls.append("ok")

    async def boom() -> None:
        raise RuntimeError("x")

    run_download_sync(ok, "t7")
    with capture_logs() as logs:
        run_download_sync(boom, "t7")
    assert calls == ["ok"]
    assert [e["event"] for e in logs] == ["background_download_failed"]
