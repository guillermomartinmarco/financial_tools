"""T1.9 (ft_indicators): manifiesto atómico por namespace y bloqueo ``O_EXCL``."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from indicators_helpers import BINANCE, series_frame, tree_digest, write_source

from finances_core.config import FinancesSettings
from finances_core.indicators_store.manifest import (
    DatasetLockedError,
    lock_path,
    manifest_path,
    read_manifest,
    write_manifest,
)
from finances_core.indicators_store.store import IndicatorStore
from finances_core.schemas.indicators import Stability, StoreEntry, StoreManifest
from finances_core.tasks import SQLTaskManager

F32 = {"rsi_14": pl.Float32}


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


@pytest.fixture
def times(settings: FinancesSettings) -> list[int]:
    return write_source(settings.PARQUET_DIR, BINANCE)


def _entry(sid: str, times: list[int], computed_at_ms: int = 1) -> StoreEntry:
    return StoreEntry(
        series_id=sid,
        spec={"name": sid.split("_", maxsplit=1)[0]},
        columns=[sid],
        column_dtypes={sid: "Float32"},
        engine_version="ft_indicators:1",
        rows=len(times),
        first_ms=times[0],
        last_ms=times[-1],
        stability=Stability(
            stable_from_ms=times[3], by_column={sid: times[3]}, rule="", reason="first_non_null"
        ),
        computed_at_ms=computed_at_ms,
    )


def _task(settings: FinancesSettings, task_id: str, status: str) -> None:
    SQLTaskManager(settings.TASKS_DB_PATH).create_task(task_id, status=status)


def test_manifest_written_atomically_per_namespace(
    settings: FinancesSettings, times: list[int]
) -> None:
    store = IndicatorStore.from_settings(settings, "ft_indicators")
    assert store.read_manifest(BINANCE) is None
    manifest = store.update_manifest(BINANCE, {"rsi_14": _entry("rsi_14", times)})
    path = manifest_path(store.dataset_dir(BINANCE), "ft_indicators")
    assert path.name == "_manifest.ft_indicators.json"
    assert store.read_manifest(BINANCE) == manifest
    assert manifest.namespace == "ft_indicators"
    assert manifest.dataset == BINANCE
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # sin temporales


def test_failed_manifest_write_keeps_previous(
    settings: FinancesSettings, times: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IndicatorStore.from_settings(settings, "ft_indicators")
    first = store.update_manifest(BINANCE, {"rsi_14": _entry("rsi_14", times)})

    def _fail(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", _fail)
    with pytest.raises(OSError, match="disk full"):
        store.update_manifest(BINANCE, {"ema_50": _entry("ema_50", times)})
    monkeypatch.undo()
    assert store.read_manifest(BINANCE) == first
    assert len(list(store.dataset_dir(BINANCE).iterdir())) == 1


def test_unrequested_entries_preserved(settings: FinancesSettings, times: list[int]) -> None:
    store = IndicatorStore.from_settings(settings, "ft_indicators")
    store.update_manifest(BINANCE, {"rsi_14": _entry("rsi_14", times, 1)})
    store.update_manifest(BINANCE, {"ema_50": _entry("ema_50", times, 2)})
    store.update_manifest(BINANCE, {"rsi_14": _entry("rsi_14", times, 3)})
    manifest = store.read_manifest(BINANCE)
    assert manifest is not None
    assert sorted(manifest.entries) == ["ema_50", "rsi_14"]
    assert manifest.entries["rsi_14"].computed_at_ms == 3
    assert manifest.entries["ema_50"].computed_at_ms == 2


def test_manifest_rejects_foreign_entries(settings: FinancesSettings, times: list[int]) -> None:
    store = IndicatorStore.from_settings(settings, "ft_indicators")
    with pytest.raises(ValueError, match="talib_"):
        store.update_manifest(BINANCE, {"talib_rsi_14": _entry("talib_rsi_14", times)})


def test_read_manifest_namespace_mismatch(tmp_path: Path) -> None:
    manifest = StoreManifest(namespace="ft_ta_lib", dataset=BINANCE)
    write_manifest(tmp_path, manifest)
    (tmp_path / "_manifest.ft_ta_lib.json").replace(tmp_path / "_manifest.ft_indicators.json")
    with pytest.raises(ValueError, match="namespace"):
        read_manifest(tmp_path, "ft_indicators")


def test_live_lock_rejects_other_task(settings: FinancesSettings, times: list[int]) -> None:
    store = IndicatorStore.from_settings(settings, "ft_indicators")
    _task(settings, "task-a", "COMPUTING")
    with store.lock(BINANCE, "task-a"):
        manifest = store.read_manifest(BINANCE)
        assert manifest is not None
        assert manifest.lock_task_id == "task-a"
        with pytest.raises(DatasetLockedError) as exc:
            store.lock(BINANCE, "task-b").__enter__()
        assert exc.value.owner_task_id == "task-a"
        assert "task-a" in str(exc.value)
    assert not lock_path(store.dataset_dir(BINANCE), "ft_indicators").exists()
    assert store.read_manifest(BINANCE) is None  # sin entradas: no queda manifiesto vacío
    store.update_manifest(BINANCE, {"rsi_14": _entry("rsi_14", times)})
    with store.lock(BINANCE, "task-a"):
        pass
    manifest = store.read_manifest(BINANCE)
    assert manifest is not None
    assert manifest.lock_task_id is None
    assert list(manifest.entries) == ["rsi_14"]


@pytest.mark.parametrize("owner_status", ["COMPLETED", "FAILED", None])
def test_stale_lock_is_replaced(
    settings: FinancesSettings, times: list[int], owner_status: str | None
) -> None:
    store = IndicatorStore.from_settings(settings, "ft_indicators")
    if owner_status is not None:
        _task(settings, "dead", owner_status)
    path = lock_path(store.dataset_dir(BINANCE), "ft_indicators")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("dead", encoding="utf-8")  # caída: el bloqueo quedó huérfano
    _task(settings, "task-b", "COMPUTING")
    with store.lock(BINANCE, "task-b"):
        assert path.read_text(encoding="utf-8") == "task-b"
    assert not path.exists()


def test_same_task_can_retake_its_lock(settings: FinancesSettings, times: list[int]) -> None:
    store = IndicatorStore.from_settings(settings, "ft_indicators")
    _task(settings, "task-a", "COMPUTING")
    path = lock_path(store.dataset_dir(BINANCE), "ft_indicators")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("task-a", encoding="utf-8")  # reanudación de la misma tarea
    with store.lock(BINANCE, "task-a"):
        assert path.read_text(encoding="utf-8") == "task-a"


def test_locks_of_both_namespaces_do_not_exclude(
    settings: FinancesSettings, times: list[int]
) -> None:
    ind = IndicatorStore.from_settings(settings, "ft_indicators")
    tal = IndicatorStore.from_settings(settings, "ft_ta_lib")
    _task(settings, "ind-task", "COMPUTING")
    _task(settings, "tal-task", "COMPUTING")
    with ind.lock(BINANCE, "ind-task"), tal.lock(BINANCE, "tal-task"):
        names = sorted(p.name for p in ind.dataset_dir(BINANCE).glob("_lock.*"))
        assert names == ["_lock.ft_indicators", "_lock.ft_ta_lib"]


def _run(store: IndicatorStore, settings: FinancesSettings, task_id: str, times: list[int]) -> None:
    _task(settings, task_id, "COMPUTING")
    with store.lock(BINANCE, task_id):
        store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
        store.update_manifest(BINANCE, {"rsi_14": _entry("rsi_14", times)})
    SQLTaskManager(settings.TASKS_DB_PATH).update_task(task_id, status="COMPLETED")


def test_crash_between_partitions_and_manifest_converges(
    settings: FinancesSettings, times: list[int], tmp_path: Path
) -> None:
    clean = IndicatorStore(
        tmp_path / "clean", "ft_indicators", tasks_db_path=settings.TASKS_DB_PATH
    )
    _run(clean, settings, "clean-task", times)

    crashed = IndicatorStore(
        tmp_path / "crashed", "ft_indicators", tasks_db_path=settings.TASKS_DB_PATH
    )
    _task(settings, "crash-task", "COMPUTING")
    lock_cm = crashed.lock(BINANCE, "crash-task")
    lock_cm.__enter__()
    # Versión antigua parcial (solo febrero) y caída antes del manifiesto y del unlock.
    crashed.write_series(BINANCE, "rsi_14", series_frame(times[48:], "rsi_14", offset=9.0), F32)
    SQLTaskManager(settings.TASKS_DB_PATH).update_task("crash-task", status="FAILED")
    assert crashed.read_manifest(BINANCE) is not None  # solo con lock_task_id
    _run(crashed, settings, "retry-task", times)

    def _data(root: Path) -> dict[str, str]:
        return {k: v for k, v in tree_digest(root).items() if k.endswith(".parquet")}

    assert _data(clean.root) == _data(crashed.root)
    m_clean, m_crashed = clean.read_manifest(BINANCE), crashed.read_manifest(BINANCE)
    assert m_clean is not None
    assert m_crashed is not None
    assert m_clean.model_dump() == m_crashed.model_dump()
    assert not any(p.name.startswith("_lock") for p in crashed.dataset_dir(BINANCE).iterdir())
