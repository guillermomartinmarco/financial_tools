"""T4.1: ``planner`` + ``service`` sobre el runner compartido e ``IndicatorStore("ft_ta_lib")``."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.indicators_store.manifest import DatasetLockedError
from finances_core.indicators_store.source import SourceNotFoundError
from finances_core.indicators_store.store import IndicatorStore
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    ComputeProgress,
    ComputeResult,
    Stability,
    StoreEntry,
)
from finances_core.schemas.market_data import TF_MS_MAP
from finances_core.schemas.ta_lib import TaLibComputeRequest, TaLibSpec
from polars.testing import assert_frame_equal

from finances_analytics.ft_ta_lib import catalog, engine, planner, service
from finances_analytics.ft_ta_lib.errors import (
    OutputOutOfRangeError,
    TaLibExecutionError,
    TaLibValidationError,
)
from finances_analytics.ft_ta_lib.model import ResolvedFunction

from .conftest import JAN_2024_MS, DatasetFactory

HOUR = TF_MS_MAP["1h"]


def _request(
    datasets: list[DatasetIdentifier], *functions: str, groups: list[str] | None = None
) -> TaLibComputeRequest:
    return TaLibComputeRequest(
        datasets=datasets,
        functions=[TaLibSpec(function=f) for f in functions],
        groups=groups or [],
    )


async def _compute(
    settings: FinancesSettings, datasets: list[DatasetIdentifier], *functions: str
) -> ComputeResult:
    prepared = service.dispatch_compute(_request(datasets, *functions), settings)
    return await service.run_compute(prepared, settings)


def _files(settings: FinancesSettings) -> dict[str, Path]:
    root = settings.INDICATORS_DIR
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*.parquet"))}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ planificación


def test_plan_expands_groups_resolves_defaults_and_dedupes(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    btc = hive_dataset(symbol="BTCUSDT")
    eth = hive_dataset(symbol="ETHUSDT")
    request = TaLibComputeRequest(
        datasets=[btc, eth, btc],
        functions=[
            TaLibSpec(function="rsi"),
            TaLibSpec(function="RSI", params={"timeperiod": 14}),
            TaLibSpec(function="bbands", params={"matype": "ema"}),
        ],
        groups=["Pattern Recognition"],
    )
    plan = planner.plan_items(request, settings, task_id="t", created_at_ms=1)
    per_dataset = 2 + 61
    assert len(plan.items) == 2 * per_dataset
    assert [i.dataset for i in plan.items[::per_dataset]] == [btc, eth]
    first = plan.items[:per_dataset]
    assert [i.series_id for i in first[:3]] == [
        "talib_rsi_14",
        "talib_bbands_20_2_2_ema",
        "talib_cdl2crows",
    ]
    rsi, bbands, crows = first[:3]
    assert rsi.spec == TaLibSpec(function="RSI", params={"timeperiod": 14})
    assert rsi.columns == {"talib_rsi_14": "Float32"}
    assert rsi.lookback == 14
    assert list(bbands.columns) == [
        "talib_bbands_20_2_2_ema_upperband",
        "talib_bbands_20_2_2_ema_middleband",
        "talib_bbands_20_2_2_ema_lowerband",
    ]
    assert crows.columns == {"talib_cdl2crows": "Int16"}


def test_plan_validates_input_columns_per_dataset(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    yf = hive_dataset(exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")
    binance = hive_dataset()
    spec = TaLibSpec(function="sma", inputs={"price": "adj_close"})
    plan = planner.plan_items(
        TaLibComputeRequest(datasets=[yf], functions=[spec]), settings, task_id="t"
    )
    assert [i.series_id for i in plan.items] == ["talib_sma_30_price_adj_close"]
    with pytest.raises(TaLibValidationError, match="adj_close"):
        planner.plan_items(
            TaLibComputeRequest(datasets=[binance], functions=[spec]), settings, task_id="t"
        )


def test_plan_is_persisted_with_ta_lib_prefix(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    prepared = service.dispatch_compute(_request([ds], "rsi"), settings)
    planner.save_plan(prepared.plan, settings)
    path = settings.SHARE_DIR / "plans" / f"ta_lib_plan_{prepared.task_id}.json"
    assert path.is_file()
    assert service.get_plan(prepared.task_id, settings) == prepared.plan


# ------------------------------------------------------------------ tarea


async def test_task_completes_with_one_item_per_pair(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    btc = hive_dataset(symbol="BTCUSDT", rows=300)
    eth = hive_dataset(symbol="ETHUSDT", rows=300, seed=3)
    seen: list[ComputeProgress] = []
    prepared = service.dispatch_compute(
        _request([btc, eth], "sma", "rsi", "cdlengulfing", "maxindex"), settings
    )
    result = await service.run_compute(prepared, settings, on_progress=seen.append)

    assert result.status == "COMPLETED", result.errors
    assert result.errors == []
    pairs = {(i.dataset.symbol, i.series_id) for i in result.items}
    assert pairs == {
        (sym, sid)
        for sym in ("BTCUSDT", "ETHUSDT")
        for sid in ("talib_sma_30", "talib_rsi_14", "talib_cdlengulfing", "talib_maxindex_30")
    }
    sma = next(i for i in result.items if i.series_id == "talib_sma_30")
    assert sma.rows == 300
    assert sma.first_ms == JAN_2024_MS
    assert sma.last_ms == JAN_2024_MS + 299 * HOUR
    assert sma.stability.stable_from_ms == JAN_2024_MS + 29 * HOUR
    rsi = next(i for i in result.items if i.series_id == "talib_rsi_14")
    assert rsi.stability.reason == "unstable_period_unknown"
    assert [p.status for p in seen][-1] == "COMPLETED"

    progress = service.get_progress(prepared.task_id, settings)
    assert progress is not None
    assert progress.status == "COMPLETED"
    assert progress.items_done == progress.items_total == 8
    assert progress.datasets_done == 2
    assert progress.stability[f"{btc.path_segment()}:talib_sma_30"] == sma.stability
    assert service.get_plan(prepared.task_id, settings) == prepared.plan


async def test_manifest_records_versions_dtypes_and_stability(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=200)
    result = await _compute(settings, [ds], "cdlhikkake", "minmaxindex", "bbands")
    store = service.get_store(settings)
    assert (store.dataset_dir(ds) / "_manifest.ft_ta_lib.json").is_file()
    manifest = service.get_manifest(ds, settings)
    assert manifest is not None
    assert manifest.namespace == "ft_ta_lib"
    assert manifest.lock_task_id is None
    cat = catalog.build_catalog()
    version = f"ta-lib {cat.talib_version} / C {cat.ta_lib_c_version}"
    assert {e.engine_version for e in manifest.entries.values()} == {version}
    hikkake = manifest.entries["talib_cdlhikkake"]
    assert hikkake.column_dtypes == {"talib_cdlhikkake": "Int16"}
    assert hikkake.spec == {"function": "CDLHIKKAKE", "params": {}, "inputs": {}}
    assert manifest.entries["talib_minmaxindex_30"].column_dtypes == {
        "talib_minmaxindex_30_minidx": "Int64",
        "talib_minmaxindex_30_maxidx": "Int64",
    }
    bbands = manifest.entries["talib_bbands_20_2_2_sma"]
    assert set(bbands.column_dtypes.values()) == {"Float32"}
    assert bbands.spec["params"] == {
        "timeperiod": 20,
        "nbdevup": 2.0,
        "nbdevdn": 2.0,
        "matype": "sma",
    }
    assert {i.series_id: i.stability for i in result.items} == {
        k: e.stability for k, e in manifest.entries.items()
    }


async def test_persisted_files_keep_their_dtypes_and_alignment(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=1000)  # ~6 semanas: 2 meses
    await _compute(settings, [ds], "cdlengulfing", "maxindex", "kama")
    files = _files(settings)
    base = "binance/market_type=spot/symbol=BTCUSDT/timeframe=1h/year=2024"
    assert sorted(files) == sorted(
        f"{base}/month={m}/{sid}.parquet"
        for m in ("01", "02")
        for sid in ("talib_cdlengulfing", "talib_maxindex_30", "talib_kama_30")
    )
    frames = {
        sid: pl.concat(
            [pl.read_parquet(p) for k, p in files.items() if k.endswith(f"/{sid}.parquet")]
        )
        for sid in ("talib_cdlengulfing", "talib_maxindex_30", "talib_kama_30")
    }
    assert frames["talib_cdlengulfing"].schema["talib_cdlengulfing"] == pl.Int16
    assert frames["talib_maxindex_30"].schema["talib_maxindex_30"] == pl.Int64
    assert frames["talib_kama_30"].schema["talib_kama_30"] == pl.Float32
    for frame in frames.values():
        assert frame.height == 1000
        assert frame["open_time_ms"].to_list() == [JAN_2024_MS + i * HOUR for i in range(1000)]
    assert frames["talib_kama_30"]["talib_kama_30"].null_count() == 30


async def test_ft_indicators_files_are_untouched(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    """``talib_*`` convive en ``FT_INDICATORS_DIR`` con ``ft_indicators`` sin modificarlo."""
    ds = hive_dataset(rows=200)
    neighbour = IndicatorStore.from_settings(settings, "ft_indicators")
    frame = pl.DataFrame(
        {
            "open_time_ms": [JAN_2024_MS + i * HOUR for i in range(200)],
            "rsi_14": [float(i) for i in range(200)],
        }
    )
    stats = neighbour.write_series(ds, "rsi_14", frame, {"rsi_14": pl.Float32})
    neighbour.update_manifest(
        ds,
        {
            "rsi_14": StoreEntry(
                series_id="rsi_14",
                spec={"name": "rsi"},
                columns=["rsi_14"],
                column_dtypes={"rsi_14": "Float32"},
                engine_version="ft_indicators:1",
                rows=stats.rows,
                first_ms=stats.first_ms,
                last_ms=stats.last_ms,
                stability=Stability(
                    stable_from_ms=None,
                    by_column={"rsi_14": None},
                    rule="x",
                    reason="not_converged",
                ),
                computed_at_ms=0,
            )
        },
    )
    before = {k: _sha(p) for k, p in _files(settings).items()}
    neighbour_manifest = neighbour.dataset_dir(ds) / "_manifest.ft_indicators.json"
    manifest_before = neighbour_manifest.read_bytes()

    result = await _compute(settings, [ds], "rsi", "cdlengulfing")
    assert result.status == "COMPLETED"
    after = {k: _sha(p) for k, p in _files(settings).items()}
    assert {k: v for k, v in after.items() if k in before} == before
    assert sorted(set(after) - set(before)) == sorted(
        f"binance/market_type=spot/symbol=BTCUSDT/timeframe=1h/year=2024/month=01/{sid}.parquet"
        for sid in ("talib_rsi_14", "talib_cdlengulfing")
    )
    assert neighbour_manifest.read_bytes() == manifest_before
    ta_manifest = service.get_manifest(ds, settings)
    assert ta_manifest is not None
    assert set(ta_manifest.entries) == {"talib_rsi_14", "talib_cdlengulfing"}


async def test_rerun_is_idempotent(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=400)
    functions = ("kama", "ht_trendline", "cdlengulfing", "minmaxindex")
    await _compute(settings, [ds], *functions)
    first_frames = {k: pl.read_parquet(p) for k, p in _files(settings).items()}
    first_bytes = {k: p.read_bytes() for k, p in _files(settings).items()}
    path = service.get_store(settings).dataset_dir(ds) / "_manifest.ft_ta_lib.json"
    first_manifest = json.loads(path.read_text(encoding="utf-8"))

    await _compute(settings, [ds], *functions)
    second = _files(settings)
    assert sorted(second) == sorted(first_frames)
    for key, p in second.items():
        assert_frame_equal(pl.read_parquet(p), first_frames[key], check_exact=True)
        assert p.read_bytes() == first_bytes[key]
    second_manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in [*first_manifest["entries"].values(), *second_manifest["entries"].values()]:
        entry.pop("computed_at_ms")
    assert second_manifest == first_manifest


async def test_insufficient_history_does_not_fail(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset(rows=10)
    result = await _compute(settings, [ds], "sma")
    assert result.status == "COMPLETED"
    (item,) = result.items
    assert "insufficient_history" in item.warnings
    assert any("insufficient_history" in w for w in result.warnings)
    (path,) = _files(settings).values()
    assert pl.read_parquet(path)["talib_sma_30"].null_count() == 10


# ------------------------------------------------------------- errores por item


def _failing(monkeypatch: pytest.MonkeyPatch, failures: dict[str, Exception]) -> None:
    real = engine.compute_function

    def fake(src: pl.DataFrame, fn: ResolvedFunction) -> engine.FunctionResult:
        if fn.name in failures:
            raise failures[fn.name]
        return real(src, fn)

    monkeypatch.setattr(engine, "compute_function", fake)


async def test_item_errors_are_recorded_and_the_task_continues(
    hive_dataset: DatasetFactory, settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = hive_dataset(rows=120)
    _failing(
        monkeypatch,
        {
            "SMA": TaLibExecutionError("SMA: fallo simulado de TA-Lib"),
            "CDLENGULFING": OutputOutOfRangeError(
                "talib_cdlengulfing", "talib_cdlengulfing", 0, 40_000
            ),
        },
    )
    prepared = service.dispatch_compute(_request([ds], "sma", "rsi", "cdlengulfing"), settings)
    result = await service.run_compute(prepared, settings)

    assert result.status == "COMPLETED"
    assert [i.series_id for i in result.items] == ["talib_rsi_14"]
    label = ds.path_segment()
    assert len(result.errors) == 2
    assert result.errors[0].startswith(f"talib_error: {label}: talib_sma_30:")
    assert result.errors[1].startswith(f"output_out_of_range: {label}: talib_cdlengulfing:")
    progress = service.get_progress(prepared.task_id, settings)
    assert progress is not None
    assert progress.errors == result.errors
    manifest = service.get_manifest(ds, settings)
    assert manifest is not None
    assert list(manifest.entries) == ["talib_rsi_14"]
    assert [k.rsplit("/", 1)[-1] for k in _files(settings)] == ["talib_rsi_14.parquet"]


async def test_item_and_dataset_errors_are_both_kept(
    hive_dataset: DatasetFactory, settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = hive_dataset(symbol="GONEUSDT")
    kept = hive_dataset(symbol="BTCUSDT")
    _failing(monkeypatch, {"SMA": TaLibExecutionError("SMA: fallo simulado")})
    prepared = service.dispatch_compute(_request([kept, gone], "sma", "rsi"), settings)
    shutil.rmtree(settings.PARQUET_DIR / "binance" / "market_type=spot" / "symbol=GONEUSDT")
    result = await service.run_compute(prepared, settings)
    assert result.status == "COMPLETED"
    assert [(i.dataset, i.series_id) for i in result.items] == [(kept, "talib_rsi_14")]
    assert sorted(e.split(":")[0] for e in result.errors) == ["dataset_not_found", "talib_error"]


async def test_io_error_fails_the_task_and_keeps_previous_files(
    hive_dataset: DatasetFactory, settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = hive_dataset(rows=100)
    await _compute(settings, [ds], "rsi")
    before = {k: p.read_bytes() for k, p in _files(settings).items()}

    def broken(*_: Any, **__: Any) -> Any:
        msg = "disco lleno"
        raise OSError(msg)

    monkeypatch.setattr(IndicatorStore, "write_series", broken)
    result = await _compute(settings, [ds], "rsi")
    assert result.status == "FAILED"
    assert any("disco lleno" in e for e in result.errors)
    assert {k: p.read_bytes() for k, p in _files(settings).items()} == before


async def test_dataset_removed_after_dispatch_is_error_and_task_continues(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    gone = hive_dataset(symbol="GONEUSDT")
    kept = hive_dataset(symbol="BTCUSDT")
    prepared = service.dispatch_compute(_request([gone, kept], "rsi"), settings)
    shutil.rmtree(settings.PARQUET_DIR / "binance" / "market_type=spot" / "symbol=GONEUSDT")
    result = await service.run_compute(prepared, settings)
    assert result.status == "COMPLETED"
    assert [i.dataset for i in result.items] == [kept]
    assert len(result.errors) == 1
    assert result.errors[0].startswith("dataset_not_found: binance/spot/GONEUSDT/1h")


# ------------------------------------------------------------------ despacho


def test_dispatch_validates_before_creating_task(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    ghost = DatasetIdentifier.parse("binance/spot/NOPE/1h")
    with pytest.raises(SourceNotFoundError):
        service.dispatch_compute(_request([ghost], "rsi"), settings)
    with pytest.raises(TaLibValidationError, match="adj_close"):
        service.dispatch_compute(
            TaLibComputeRequest(
                datasets=[ds], functions=[TaLibSpec(function="sma", inputs={"price": "adj_close"})]
            ),
            settings,
        )
    with pytest.raises(TaLibValidationError, match="Pattern Recognition"):
        service.dispatch_compute(_request([ds], groups=["Nope"]), settings)
    with pytest.raises(TaLibValidationError, match="MAVP"):
        service.dispatch_compute(_request([ds], "mavp"), settings)
    assert not settings.TASKS_DB_PATH.exists()


def test_dispatch_rejects_dataset_locked_by_live_task(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    owner = service.dispatch_compute(_request([ds], "rsi"), settings)  # PENDING = viva
    store = service.get_store(settings)
    with store.lock(ds, owner.task_id):
        with pytest.raises(DatasetLockedError) as info:
            service.dispatch_compute(_request([ds], "ema"), settings)
        assert info.value.owner_task_id == owner.task_id


def test_ft_indicators_lock_does_not_interfere(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    owner = service.dispatch_compute(_request([ds], "rsi"), settings)  # tarea viva
    neighbour = IndicatorStore.from_settings(settings, "ft_indicators")
    with neighbour.lock(ds, owner.task_id):
        assert service.dispatch_compute(_request([ds], "ema"), settings).task_id


def test_stale_lock_of_finished_or_unknown_task_is_ignored(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    lock = service.get_store(settings).dataset_dir(ds) / "_lock.ft_ta_lib"
    lock.parent.mkdir(parents=True)
    lock.write_text("0192f000-0000-7000-8000-0000000000aa", encoding="utf-8")
    assert service.dispatch_compute(_request([ds], "rsi"), settings).task_id


# ------------------------------------------------------------------ catálogo


def test_catalog_filters_by_group_and_function() -> None:
    full = service.catalog()
    assert len(full.functions) == len(catalog.build_catalog().functions)
    patterns = service.catalog(group="pattern recognition")
    assert len(patterns.functions) == 61
    assert list(patterns.groups) == ["Pattern Recognition"]
    assert {e.group for e in patterns.functions} == {"Pattern Recognition"}
    rsi = service.catalog(function="rsi")
    assert [e.function for e in rsi.functions] == ["RSI"]
    assert rsi.groups == {"Momentum Indicators": ["RSI"]}
    assert service.catalog(group="Overlap Studies", function="rsi").functions == []
    with pytest.raises(TaLibValidationError, match="grupos válidos"):
        service.catalog(group="Nope")
    with pytest.raises(TaLibValidationError, match="grupos válidos"):
        service.catalog(function="NOPE")
