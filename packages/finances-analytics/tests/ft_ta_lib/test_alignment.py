"""T3.3: alineación 1:1, calentamiento, historia insuficiente, idempotencia y configuración
global de TA-Lib intacta (RF-6, RF-10)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl
import pytest
import talib
from finances_core.indicators_store.store import IndicatorStore
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.ta_lib import TaLibSpec
from polars.testing import assert_frame_equal

from finances_analytics.ft_ta_lib import catalog, engine, resolver
from finances_analytics.ft_ta_lib.model import UNSTABLE_FLAG, ResolvedFunction

from .conftest import ohlcv_frame

SUPPORTED: list[ResolvedFunction] = [
    resolver.resolve(TaLibSpec(function=name))
    for name, meta in catalog.function_table().items()
    if meta.supported
]
DS = DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")


def _holed(rows: int) -> pl.DataFrame:
    frame = ohlcv_frame(max(rows, 1), seed=9).head(rows)
    return frame.with_columns(
        pl.when(pl.int_range(pl.len()).is_in([3, 150]))
        .then(None)
        .otherwise(pl.col("close"))
        .alias("close")
    )


@pytest.mark.parametrize("rows", [0, 1, 5, 300])
def test_same_rows_and_open_time_ms_as_source_for_whole_catalog(rows: int) -> None:
    frame = _holed(rows)
    for fn in SUPPORTED:
        out = engine.compute_function(frame, fn).frame
        assert out.columns == ["open_time_ms", *fn.columns.values()], fn.name
        assert out["open_time_ms"].to_list() == frame["open_time_ms"].to_list(), fn.name


def test_insufficient_history_gives_null_columns_and_warning() -> None:
    frame = ohlcv_frame(10)
    for name in ("RSI", "MACD", "CDLMORNINGSTAR", "MINMAXINDEX", "HT_TRENDMODE"):
        fn = resolver.resolve(TaLibSpec(function=name))
        assert fn.lookback >= frame.height
        result = engine.compute_function(frame, fn)
        assert "insufficient_history" in result.warnings
        for column in fn.columns.values():
            assert result.frame[column].null_count() == frame.height, (name, column)


def test_enough_history_has_no_insufficient_warning() -> None:
    fn = resolver.resolve(TaLibSpec(function="RSI"))
    result = engine.compute_function(ohlcv_frame(fn.lookback + 1), fn)
    assert result.warnings == ()
    assert result.frame["talib_rsi_14"].null_count() == fn.lookback


def _digest(paths: list[Path]) -> list[str]:
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]


def test_two_runs_give_identical_content(tmp_path: Path) -> None:
    frame = _holed(600)
    store = IndicatorStore(tmp_path / "ind", "ft_ta_lib")
    first: dict[str, list[str]] = {}
    for run in range(2):
        for fn in SUPPORTED:
            a = engine.compute_function(frame, fn)
            b = engine.compute_function(frame, fn)
            assert_frame_equal(a.frame, b.frame, check_exact=True)
            assert a.stability == b.stability
            store.write_series(DS, fn.series_id, a.frame, fn.dtypes)
            digest = _digest(store.series_files(DS, fn.series_id))
            if run == 0:
                first[fn.series_id] = digest
            else:
                assert digest == first[fn.series_id], fn.series_id


def test_global_talib_configuration_is_untouched_by_a_task(tmp_path: Path) -> None:
    before = engine.global_config()
    unstable = [n for n, m in catalog.function_table().items() if UNSTABLE_FLAG in m.flags]
    assert len(before) == 1 + len(unstable)
    assert all(v == 0 for v in before.values())
    store = IndicatorStore(tmp_path / "ind", "ft_ta_lib")
    frame = ohlcv_frame(300)
    for fn in SUPPORTED:
        store.write_series(DS, fn.series_id, engine.compute_function(frame, fn).frame, fn.dtypes)
    assert engine.global_config() == before
    assert talib.get_compatibility() == 0
