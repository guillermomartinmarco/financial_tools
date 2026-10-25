"""T1.11 (ft_indicators): ``run_compute_task``/``dispatch`` sobre ``tasks_lifecycle``."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence

import polars as pl
import pytest
from indicators_helpers import BINANCE, YFINANCE, series_frame, tree_digest, write_source
from pydantic import BaseModel

from finances_core.config import FinancesSettings
from finances_core.indicators_store.runner import (
    DatasetOutcome,
    create_compute_task,
    dispatch,
    get_progress,
    get_result,
    run_compute_task,
)
from finances_core.indicators_store.source import (
    InvalidSourceError,
    load_source,
    validate_source,
)
from finances_core.indicators_store.store import IndicatorStore
from finances_core.marketdata.plans import load_plan
from finances_core.marketdata.tasks_lifecycle import TaskNotFinishedError
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    ComputeItemResult,
    ComputeProgress,
    Stability,
    StoreEntry,
)
from finances_core.tasks import SQLTaskManager


class _Plan(BaseModel):
    task_id: str
    items: list[str]


def _ds(i: int) -> DatasetIdentifier:
    return BINANCE.model_copy(update={"symbol": f"SYM{i}"})


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


@pytest.fixture
def store(settings: FinancesSettings) -> IndicatorStore:
    return IndicatorStore.from_settings(settings, "ft_indicators")


def _fake_compute(store: IndicatorStore, settings: FinancesSettings):
    """``compute_dataset`` falso: carga la fuente, escribe una serie por item y su entrada."""

    def compute(ds: DatasetIdentifier, items: Sequence[str]) -> DatasetOutcome:
        src = validate_source(load_source(settings, ds, ["close"]).frame, ["close"])
        times = src.frame["open_time_ms"].to_list()
        results, entries = [], {}
        for sid in items:
            stats = store.write_series(ds, sid, series_frame(times, sid), {sid: pl.Float32})
            stability = Stability(
                stable_from_ms=times[3], by_column={sid: times[3]}, rule="", reason="first_non_null"
            )
            results.append(
                ComputeItemResult(
                    dataset=ds,
                    series_id=sid,
                    columns=[sid],
                    rows=stats.rows,
                    first_ms=stats.first_ms,
                    last_ms=stats.last_ms,
                    stability=stability,
                    partitions_written=stats.partitions_written,
                    partitions_removed=stats.partitions_removed,
                    warnings=["insufficient_history"] if sid == "sma_500" else [],
                )
            )
            entries[sid] = StoreEntry(
                series_id=sid,
                spec={},
                columns=[sid],
                column_dtypes={sid: "Float32"},
                engine_version="test:1",
                rows=stats.rows,
                first_ms=stats.first_ms,
                last_ms=stats.last_ms,
                stability=stability,
                computed_at_ms=0,
            )
        return DatasetOutcome(
            items=results, entries=entries, rows_computed=len(times), warnings=["null_inputs=0"]
        )

    return compute


async def _run(
    store: IndicatorStore,
    settings: FinancesSettings,
    task_id: str,
    items_by_dataset: dict[DatasetIdentifier, list[str]],
    compute=None,
    **kw: object,
):
    create_compute_task(
        task_id,
        datasets_total=len(items_by_dataset),
        items_total=sum(len(v) for v in items_by_dataset.values()),
        settings=settings,
    )
    plan = _Plan(task_id=task_id, items=[s for v in items_by_dataset.values() for s in v])
    return await run_compute_task(
        task_id,
        plan,
        items_by_dataset,
        compute or _fake_compute(store, settings),
        store,
        settings,
        plan_prefix="indicators_plan",
        **kw,
    )


async def test_phases_progress_and_result(
    store: IndicatorStore, settings: FinancesSettings
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)
    write_source(settings.PARQUET_DIR, YFINANCE)
    phases: list[str] = []
    progress: list[ComputeProgress] = []
    assert get_progress("t1", settings) is None
    result = await _run(
        store,
        settings,
        "t1",
        {BINANCE: ["rsi_14", "sma_500"], YFINANCE: ["rsi_14"]},
        on_phase=phases.append,
        on_progress=progress.append,
    )
    assert phases == ["PLANNING", "COMPUTING", "COMPLETED"]
    statuses = [p.status for p in progress]
    order = ["PENDING", "PLANNING", "COMPUTING", "COMPLETED"]
    assert [order.index(s) for s in statuses] == sorted(order.index(s) for s in statuses)
    assert result.status == "COMPLETED"
    assert len(result.items) == 3
    assert result.errors == []
    assert "binance/spot/BTCUSDT/1h:sma_500: insufficient_history" in result.warnings
    assert sorted(result.output_dirs) == sorted(
        str(store.dataset_dir(d)) for d in (BINANCE, YFINANCE)
    )

    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task("t1")
    assert row is not None
    assert row["status"] == "COMPLETED"
    final = get_progress("t1", settings)
    assert final is not None
    assert (final.datasets_done, final.items_done, final.items_total) == (2, 3, 3)
    assert final.rows_computed == 200
    assert final.partitions_written == 6
    assert set(final.stability) == {
        "binance/spot/BTCUSDT/1h:rsi_14",
        "binance/spot/BTCUSDT/1h:sma_500",
        "yfinance/equity/AAPL/1h:rsi_14",
    }
    assert get_result("t1", settings) == result
    assert load_plan("t1", _Plan, settings, prefix="indicators_plan") is not None
    manifest = store.read_manifest(BINANCE)
    assert manifest is not None
    assert sorted(manifest.entries) == ["rsi_14", "sma_500"]
    assert manifest.lock_task_id is None


async def test_invalid_source_is_error_and_task_continues(
    store: IndicatorStore, settings: FinancesSettings
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)
    write_source(settings.PARQUET_DIR, YFINANCE)
    base = _fake_compute(store, settings)

    def compute(ds: DatasetIdentifier, items: Sequence[str]) -> DatasetOutcome:
        if ds == YFINANCE:
            raise InvalidSourceError("open_time_ms no es estrictamente creciente")
        return base(ds, items)

    missing = BINANCE.model_copy(update={"symbol": "GONE"})
    result = await _run(
        store,
        settings,
        "t2",
        {BINANCE: ["rsi_14"], YFINANCE: ["rsi_14"], missing: ["rsi_14"]},
        compute,
    )
    assert result.status == "COMPLETED"
    assert len(result.items) == 1
    assert any(e.startswith("invalid_source: yfinance/equity/AAPL/1h") for e in result.errors)
    assert any(e.startswith("dataset_not_found: binance/spot/GONE/1h") for e in result.errors)
    progress = get_progress("t2", settings)
    assert progress is not None
    assert progress.datasets_done == 3
    assert store.read_manifest(YFINANCE) is None  # sin entradas: no queda manifiesto vacío
    assert not list(store.dataset_dir(YFINANCE).glob("_lock.*"))


async def test_io_error_fails_task_and_keeps_previous_files(
    store: IndicatorStore, settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)
    first = await _run(store, settings, "t3a", {BINANCE: ["rsi_14"]})
    assert first.status == "COMPLETED"
    before = tree_digest(store.root)

    def _disk_full(self: pl.DataFrame, *a: object, **k: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", _disk_full)
    result = await _run(store, settings, "t3b", {BINANCE: ["rsi_14"]})
    monkeypatch.undo()
    assert result.status == "FAILED"
    assert any("No space left" in e for e in result.errors)
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task("t3b")
    assert row is not None
    assert row["status"] == "FAILED"
    after = tree_digest(store.root)
    assert {k: v for k, v in after.items() if k.endswith(".parquet")} == {
        k: v for k, v in before.items() if k.endswith(".parquet")
    }
    manifest = store.read_manifest(BINANCE)
    assert manifest is not None
    assert manifest.lock_task_id is None
    assert sorted(manifest.entries) == ["rsi_14"]


async def test_unexpected_exception_fails_task(
    store: IndicatorStore, settings: FinancesSettings
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)

    def compute(ds: DatasetIdentifier, items: Sequence[str]) -> DatasetOutcome:
        raise RuntimeError("bug")

    result = await _run(store, settings, "t4", {BINANCE: ["rsi_14"]}, compute)
    assert result.status == "FAILED"
    assert any("bug" in e for e in result.errors)


async def test_locked_dataset_is_reported(
    store: IndicatorStore, settings: FinancesSettings
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)
    SQLTaskManager(settings.TASKS_DB_PATH).create_task("owner", status="COMPUTING")
    with store.lock(BINANCE, "owner"):
        result = await _run(store, settings, "t5", {BINANCE: ["rsi_14"]})
    assert result.status == "COMPLETED"
    assert any(e.startswith("dataset_locked:") and "owner" in e for e in result.errors)


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_peak_concurrency_equals_limit(
    store: IndicatorStore, settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    monkeypatch.setenv("FT_INDICATORS_MAX_CONCURRENT", str(limit))
    settings = FinancesSettings()
    active, peak = 0, 0
    guard = threading.Lock()

    def compute(ds: DatasetIdentifier, items: Sequence[str]) -> DatasetOutcome:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with guard:
            active -= 1
        return DatasetOutcome(items=[], entries={}, rows_computed=0, warnings=[])

    datasets = {_ds(i): ["rsi_14"] for i in range(5)}
    result = await _run(store, settings, f"t6-{limit}", datasets, compute)
    assert result.status == "COMPLETED"
    assert peak == limit


async def test_get_result_unfinished_and_missing(settings: FinancesSettings) -> None:
    assert get_result("nope", settings) is None
    create_compute_task("t7", datasets_total=1, items_total=1, settings=settings)
    with pytest.raises(TaskNotFinishedError):
        get_result("t7", settings)
    progress = get_progress("t7", settings)
    assert progress is not None
    assert progress.status == "PENDING"
    assert (progress.datasets_total, progress.items_total) == (1, 1)


def test_dispatch_contract() -> None:
    resp = dispatch("0190-abc", "http://testserver/", "/indicators")
    assert resp.status == "ACCEPTED"
    assert resp.task_id == "0190-abc"
    assert resp.poll_url == "http://testserver/indicators/status/0190-abc"
    assert resp.plan_url == "http://testserver/indicators/plan/0190-abc"
    assert resp.result_url == "http://testserver/indicators/result/0190-abc"
    assert dispatch("x", "http://h", "ta-lib").poll_url == "http://h/ta-lib/status/x"
