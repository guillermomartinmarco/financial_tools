"""T5.1: servicio sobre ``indicators_store.runner`` + ``IndicatorStore("ft_indicators")``."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.indicators_store.manifest import DatasetLockedError
from finances_core.indicators_store.source import SourceNotFoundError
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    ComputeProgress,
    ComputeResult,
    IndicatorComputeRequest,
    IndicatorSpec,
)
from finances_core.schemas.market_data import TF_MS_MAP
from polars.testing import assert_frame_equal

from finances_analytics.ft_indicators import service
from finances_analytics.ft_indicators.errors import IndicatorValidationError

from .conftest import JAN_2024_MS, DatasetFactory

HOUR = TF_MS_MAP["1h"]


def _request(ds: DatasetIdentifier, *names: str) -> IndicatorComputeRequest:
    return IndicatorComputeRequest(
        datasets=[ds], indicators=[IndicatorSpec.model_validate({"name": n}) for n in names]
    )


async def _compute(ds: DatasetIdentifier, settings: FinancesSettings, *names: str) -> ComputeResult:
    prepared = service.dispatch_compute(_request(ds, *names), settings)
    return await service.run_compute(prepared, settings)


def _files(settings: FinancesSettings) -> dict[str, Path]:
    root = settings.INDICATORS_DIR
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*.parquet"))}


async def test_task_completes_with_item_results_and_stability(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=300)
    seen: list[ComputeProgress] = []
    prepared = service.dispatch_compute(_request(ds, "ema", "rsi", "obv"), settings)
    result = await service.run_compute(prepared, settings, on_progress=seen.append)

    assert result.status == "COMPLETED"
    assert result.errors == []
    assert [i.series_id for i in result.items] == ["ema_20", "rsi_14", "obv"]
    ema, rsi, obv = result.items
    assert ema.rows == 300
    assert ema.first_ms == JAN_2024_MS
    assert ema.last_ms == JAN_2024_MS + 299 * HOUR
    assert ema.stability.stable_from_ms == JAN_2024_MS + 158 * HOUR  # RF-16 / T2.8
    assert rsi.stability.stable_from_ms == JAN_2024_MS + 201 * HOUR
    assert obv.stability.stable_from_ms is None
    assert obv.stability.reason == "non_decaying_seed"
    assert ema.partitions_written == 1
    assert result.output_dirs == [str(service.get_store(settings).dataset_dir(ds))]
    assert [p.status for p in seen][-1] == "COMPLETED"

    progress = service.get_progress(prepared.task_id, settings)
    assert progress is not None
    assert progress.status == "COMPLETED"
    assert progress.items_done == progress.items_total == 3
    assert progress.rows_computed == 300
    key = f"{ds.path_segment()}:ema_20"
    assert progress.stability[key] == ema.stability
    assert service.get_plan(prepared.task_id, settings) == prepared.plan


async def test_manifest_is_coherent_with_convergence(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=500)
    result = await _compute(ds, settings, "macd", "adx")
    manifest_path = service.get_store(settings).dataset_dir(ds) / "_manifest.ft_indicators.json"
    assert manifest_path.is_file()

    manifest = service.get_manifest(ds, settings)
    assert manifest is not None
    assert manifest.namespace == "ft_indicators"
    assert manifest.lock_task_id is None
    macd = manifest.entries["macd_12_26_9"]
    assert macd.stability.by_column == {
        "macd_12_26_9_line": JAN_2024_MS + 205 * HOUR,
        "macd_12_26_9_signal": JAN_2024_MS + 267 * HOUR,
        "macd_12_26_9_hist": JAN_2024_MS + 267 * HOUR,
    }
    assert manifest.entries["adx_14"].stability.stable_from_ms == JAN_2024_MS + 387 * HOUR
    assert macd.column_dtypes == dict.fromkeys(macd.columns, "Float32")
    assert macd.engine_version == "ft_indicators:1"
    assert macd.spec == {
        "name": "macd",
        "params": {"fast": 12, "slow": 26, "signal": 9, "source": "close"},
    }
    assert {i.series_id: i.stability for i in result.items} == {
        k: e.stability for k, e in manifest.entries.items()
    }


async def test_persisted_files_are_float32_and_aligned(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=1000)  # ~6 semanas: 2 meses
    await _compute(ds, settings, "bb")
    files = _files(settings)
    assert sorted(files) == [
        "binance/market_type=spot/symbol=BTCUSDT/timeframe=1h/year=2024/month=01/bb_20_2.parquet",
        "binance/market_type=spot/symbol=BTCUSDT/timeframe=1h/year=2024/month=02/bb_20_2.parquet",
    ]
    frame = pl.concat([pl.read_parquet(p) for p in files.values()])
    assert frame.height == 1000
    assert frame.schema["open_time_ms"] == pl.Int64
    assert all(frame.schema[c] == pl.Float32 for c in frame.columns[1:])
    assert frame["bb_20_2_middle"].null_count() == 19


async def test_rerun_is_idempotent(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=400)
    await _compute(ds, settings, "rsi", "macd", "obv")
    first_files = {k: pl.read_parquet(p) for k, p in _files(settings).items()}
    first_bytes = {k: p.read_bytes() for k, p in _files(settings).items()}
    store = service.get_store(settings)
    first_manifest = json.loads(
        (store.dataset_dir(ds) / "_manifest.ft_indicators.json").read_text()
    )

    await _compute(ds, settings, "rsi", "macd", "obv")
    second = _files(settings)
    assert sorted(second) == sorted(first_files)
    for key, path in second.items():
        assert_frame_equal(pl.read_parquet(path), first_files[key], check_exact=True)
        assert path.read_bytes() == first_bytes[key]
    second_manifest = json.loads(
        (store.dataset_dir(ds) / "_manifest.ft_indicators.json").read_text()
    )
    for entry in [*first_manifest["entries"].values(), *second_manifest["entries"].values()]:
        entry.pop("computed_at_ms")
    assert second_manifest == first_manifest


async def test_new_source_candles_are_recomputed(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=200)
    await _compute(ds, settings, "sma")
    hive_dataset(rows=50, start_ms=JAN_2024_MS + 200 * HOUR, seed=3)  # velas nuevas
    result = await _compute(ds, settings, "sma")
    (item,) = result.items
    assert item.rows == 250
    assert item.last_ms == JAN_2024_MS + 249 * HOUR
    frame = pl.concat([pl.read_parquet(p) for p in _files(settings).values()])
    assert frame.height == 250
    assert frame["sma_20"].null_count() == 19


async def test_unrequested_indicators_are_kept(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=100)
    await _compute(ds, settings, "rsi")
    rsi_bytes = {k: p.read_bytes() for k, p in _files(settings).items()}
    await _compute(ds, settings, "ema")
    manifest = service.get_manifest(ds, settings)
    assert manifest is not None
    assert set(manifest.entries) == {"rsi_14", "ema_20"}
    for key, data in rsi_bytes.items():
        assert (settings.INDICATORS_DIR / key).read_bytes() == data


async def test_insufficient_history_does_not_fail(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=10)
    result = await _compute(ds, settings, "rsi")
    assert result.status == "COMPLETED"
    (item,) = result.items
    assert "insufficient_history" in item.warnings
    assert "not_converged" in item.warnings
    assert any("insufficient_history" in w for w in result.warnings)
    (path,) = _files(settings).values()
    assert pl.read_parquet(path)["rsi_14"].null_count() == 10


async def test_dataset_removed_after_dispatch_is_error_and_task_continues(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    gone = hive_dataset(symbol="GONEUSDT")
    kept = hive_dataset(symbol="BTCUSDT")
    request = IndicatorComputeRequest(datasets=[gone, kept], indicators=[IndicatorSpec(name="rsi")])
    prepared = service.dispatch_compute(request, settings)
    shutil.rmtree(settings.PARQUET_DIR / "binance" / "market_type=spot" / "symbol=GONEUSDT")
    result = await service.run_compute(prepared, settings)
    assert result.status == "COMPLETED"
    assert [i.dataset for i in result.items] == [kept]
    assert len(result.errors) == 1
    assert result.errors[0].startswith("dataset_not_found: binance/spot/GONEUSDT/1h")


def test_dispatch_validates_before_creating_task(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    ghost = DatasetIdentifier.parse("binance/spot/NOPE/1h")
    with pytest.raises(SourceNotFoundError):
        service.dispatch_compute(_request(ghost, "rsi"), settings)
    with pytest.raises(IndicatorValidationError):
        service.dispatch_compute(
            IndicatorComputeRequest(
                datasets=[ds],
                indicators=[IndicatorSpec(name="sma", params={"source": "adj_close"})],
            ),
            settings,
        )
    assert not settings.TASKS_DB_PATH.exists()


def test_dispatch_rejects_dataset_locked_by_live_task(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    owner = service.dispatch_compute(_request(ds, "rsi"), settings)  # PENDING = viva
    store = service.get_store(settings)
    with store.lock(ds, owner.task_id):
        with pytest.raises(DatasetLockedError) as info:
            service.dispatch_compute(_request(ds, "ema"), settings)
        assert info.value.owner_task_id == owner.task_id


def test_stale_lock_of_finished_or_unknown_task_is_ignored(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    lock = service.get_store(settings).dataset_dir(ds) / "_lock.ft_indicators"
    lock.parent.mkdir(parents=True)
    lock.write_text("0192f000-0000-7000-8000-0000000000aa", encoding="utf-8")
    prepared = service.dispatch_compute(_request(ds, "rsi"), settings)
    assert prepared.task_id


def test_catalog_is_published() -> None:
    entries = service.catalog()
    assert [e.name for e in entries][:3] == ["sma", "ema", "wma"]
    assert len(entries) == 12
