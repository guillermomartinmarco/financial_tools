"""T3.4: metadatos de estabilidad (RF-17) en motor, catálogo, manifiesto, progreso y resultado.

El manifiesto, el progreso y el resultado los construyen piezas compartidas de
``finances_core.indicators_store`` (``IndicatorStore.update_manifest`` y el runner); aquí se
alimentan con el ``Stability`` del motor mediante un ``compute_dataset`` mínimo de test (el
servicio real llega en T4.1 y reutiliza exactamente estas piezas).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from finances_core.config import FinancesSettings
from finances_core.indicators_store.runner import (
    DatasetOutcome,
    create_compute_task,
    get_progress,
    get_result,
    run_compute_task,
)
from finances_core.indicators_store.source import load_source, validate_source
from finances_core.indicators_store.store import IndicatorStore
from finances_core.indicators_store.view import ExportValidationError, build_wide_view
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    ComputeItemResult,
    SeriesExportRequest,
    StoreEntry,
)
from finances_core.schemas.ta_lib import TaLibPlan, TaLibPlanItem, TaLibSpec

from finances_analytics.ft_ta_lib import catalog, engine, resolver
from finances_analytics.ft_ta_lib.model import ResolvedFunction

from .conftest import DatasetFactory, ohlcv_frame


def _fn(name: str, **params: int | float | str) -> ResolvedFunction:
    return resolver.resolve(TaLibSpec(function=name, params=params))


def test_sma_is_stable_from_its_first_non_null_row() -> None:
    frame = ohlcv_frame(100)
    fn = _fn("SMA", timeperiod=20)
    stab = engine.compute_function(frame, fn).stability
    first = frame["open_time_ms"][19]
    assert stab.reason == "first_non_null"
    assert stab.rule == "first_non_null"
    assert stab.stable_from_ms == first
    assert stab.by_column == {"talib_sma_20": first}


def test_multi_output_stable_from_is_the_latest_column() -> None:
    frame = ohlcv_frame(100)
    fn = _fn("MACD")  # sin unstable period en TA-Lib
    stab = engine.compute_function(frame, fn).stability
    assert stab.reason == "first_non_null"
    assert stab.stable_from_ms == max(v for v in stab.by_column.values() if v is not None)
    assert stab.stable_from_ms == frame["open_time_ms"][fn.lookback]


@pytest.mark.parametrize("name", ["EMA", "RSI", "KAMA", "HT_TRENDMODE", "MAMA"])
def test_unstable_period_functions_have_null_stable_from(name: str) -> None:
    fn = _fn(name)
    stab = engine.compute_function(ohlcv_frame(200), fn).stability
    assert stab.stable_from_ms is None
    assert stab.reason == "unstable_period_unknown"
    assert set(stab.by_column) == set(fn.columns.values())
    assert all(v is None for v in stab.by_column.values())


def test_all_null_column_is_not_converged() -> None:
    stab = engine.compute_function(ohlcv_frame(5), _fn("SMA")).stability
    assert stab.stable_from_ms is None
    assert stab.reason == "not_converged"


def test_catalog_publishes_the_stability_rule() -> None:
    entries = {e.function: e for e in catalog.build_catalog().functions}
    assert entries["SMA"].stability_rule == "first_non_null"
    for name in ("EMA", "RSI", "KAMA"):
        assert entries[name].stability_rule == "unstable_period_unknown"


@pytest.fixture
def computed(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> tuple[DatasetIdentifier, IndicatorStore, str]:
    """Tarea real sobre el runner compartido con ``talib_sma_20`` y ``talib_rsi_14``."""
    ds = hive_dataset(rows=120)
    store = IndicatorStore.from_settings(settings, "ft_ta_lib")
    functions = [_fn("SMA", timeperiod=20), _fn("RSI")]

    def compute_dataset(
        dsi: DatasetIdentifier, items: Sequence[ResolvedFunction]
    ) -> DatasetOutcome:
        cols = list(dict.fromkeys(c for fn in items for c in fn.source_columns))
        source = validate_source(load_source(settings, dsi, cols).frame, cols).frame
        results: list[ComputeItemResult] = []
        entries: dict[str, StoreEntry] = {}
        for fn in items:
            out = engine.compute_function(source, fn)
            stats = store.write_series(dsi, fn.series_id, out.frame, fn.dtypes)
            results.append(
                ComputeItemResult(
                    dataset=dsi,
                    series_id=fn.series_id,
                    columns=list(fn.columns.values()),
                    rows=stats.rows,
                    first_ms=stats.first_ms,
                    last_ms=stats.last_ms,
                    stability=out.stability,
                    partitions_written=stats.partitions_written,
                    partitions_removed=stats.partitions_removed,
                    warnings=list(out.warnings),
                )
            )
            entries[fn.series_id] = StoreEntry(
                series_id=fn.series_id,
                spec=fn.spec().model_dump(),
                columns=list(fn.columns.values()),
                column_dtypes=fn.dtypes,
                engine_version="test",
                rows=stats.rows,
                first_ms=stats.first_ms,
                last_ms=stats.last_ms,
                stability=out.stability,
                computed_at_ms=0,
            )
        return DatasetOutcome(items=results, entries=entries, rows_computed=source.height)

    task_id = "0192f0c4-7d2e-7a3b-8c4d-5e6f7a8b9c0d"
    create_compute_task(task_id, datasets_total=1, items_total=2, settings=settings)
    plan = TaLibPlan(
        task_id=task_id,
        created_at_ms=0,
        items=[
            TaLibPlanItem(
                dataset=ds,
                series_id=fn.series_id,
                spec=fn.spec(),
                columns=fn.dtypes,
                lookback=fn.lookback,
            )
            for fn in functions
        ],
    )

    result = asyncio.run(
        run_compute_task(
            task_id,
            plan,
            {ds: functions},
            compute_dataset,
            store,
            settings,
            plan_prefix="ta_lib_plan",
        )
    )
    assert result.status == "COMPLETED", result.errors
    return ds, store, task_id


def test_stability_in_manifest_progress_and_result(
    computed: tuple[DatasetIdentifier, IndicatorStore, str], settings: FinancesSettings
) -> None:
    ds, store, task_id = computed
    manifest = store.read_manifest(ds)
    assert manifest is not None
    assert manifest.namespace == "ft_ta_lib"
    sma = manifest.entries["talib_sma_20"].stability
    rsi = manifest.entries["talib_rsi_14"].stability
    assert sma.reason == "first_non_null"
    assert sma.stable_from_ms is not None
    assert rsi.reason == "unstable_period_unknown"
    assert rsi.stable_from_ms is None

    key = f"{ds.path_segment()}:talib_rsi_14"
    progress = get_progress(task_id, settings)
    assert progress is not None
    assert progress.stability[key] == rsi
    assert progress.stability[f"{ds.path_segment()}:talib_sma_20"] == sma
    result = get_result(task_id, settings)
    assert result is not None
    assert {i.series_id: i.stability for i in result.items} == {
        "talib_sma_20": sma,
        "talib_rsi_14": rsi,
    }


def test_stability_drop_on_rsi_is_validation(
    computed: tuple[DatasetIdentifier, IndicatorStore, str], settings: FinancesSettings
) -> None:
    ds, store, _task_id = computed
    with pytest.raises(ExportValidationError, match="talib_rsi_14"):
        build_wide_view(
            store,
            ds,
            SeriesExportRequest(series=["talib_rsi_14"], stability="drop"),
            settings=settings,
        )
    view = build_wide_view(
        store,
        ds,
        SeriesExportRequest(series=["talib_sma_20"], stability="drop"),
        settings=settings,
    )
    assert view is not None
