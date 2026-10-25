"""T1.8 (ft_indicators): ``IndicatorStore.write_series``/``scan_series`` con dos namespaces."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
import pytest
from indicators_helpers import BINANCE, HOUR_MS, YFINANCE, series_frame, tree_digest, write_source

from finances_core.config import ConfigError, FinancesSettings
from finances_core.indicators_store.namespaces import NamespaceViolationError
from finances_core.indicators_store.store import (
    IndicatorStore,
    SeriesDtypeError,
    SeriesNotFoundError,
    dtype_name,
)

F32 = {"rsi_14": pl.Float32}


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


@pytest.fixture
def times(settings: FinancesSettings) -> list[int]:
    return write_source(settings.PARQUET_DIR, BINANCE)  # 100 velas 1h: 30-ene → 3-feb


@pytest.fixture
def store(settings: FinancesSettings) -> IndicatorStore:
    return IndicatorStore.from_settings(settings, "ft_indicators")


def _month_file(store: IndicatorStore, month: int, name: str) -> Path:
    return store.dataset_dir(BINANCE) / "year=2024" / f"month={month:02d}" / name


def test_layout_and_rows_per_month(store: IndicatorStore, times: list[int]) -> None:
    stats = store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    jan, feb = _month_file(store, 1, "rsi_14.parquet"), _month_file(store, 2, "rsi_14.parquet")
    assert store.dataset_dir(BINANCE) == (
        store.root / "binance" / "market_type=spot" / "symbol=BTCUSDT" / "timeframe=1h"
    )
    assert jan.is_file()
    assert feb.is_file()
    jan_rows, feb_rows = pl.read_parquet(jan), pl.read_parquet(feb)
    assert jan_rows["open_time_ms"].to_list() == times[:48]  # 30 y 31 de enero
    assert feb_rows["open_time_ms"].to_list() == times[48:]
    assert jan_rows.columns == ["open_time_ms", "rsi_14"]
    assert jan_rows["rsi_14"].null_count() == 3  # calentamiento conservado como nulo
    assert (stats.rows, stats.partitions_written, stats.partitions_removed) == (100, 2, 0)
    assert (stats.first_ms, stats.last_ms) == (times[0], times[-1])


def test_zstd_and_statistics(store: IndicatorStore, times: list[int]) -> None:
    store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    meta = pq.ParquetFile(_month_file(store, 1, "rsi_14.parquet")).metadata
    col = meta.row_group(0).column(1)
    assert col.compression == "ZSTD"
    assert col.statistics is not None


def test_dtypes_respected_float32_int16_int64(settings: FinancesSettings, times: list[int]) -> None:
    store = IndicatorStore.from_settings(settings, "ft_ta_lib")
    n = len(times)
    df = pl.DataFrame(
        {
            "open_time_ms": times,
            "talib_x_real": [float(i) for i in range(n)],
            "talib_x_pattern": [(-200 if i % 2 else 200) for i in range(n)],
            "talib_x_index": times,
        }
    )
    dtypes = {
        "talib_x_real": pl.Float32,
        "talib_x_pattern": pl.Int16,
        "talib_x_index": pl.Int64,
    }
    store.write_series(BINANCE, "talib_x", df, dtypes)
    back = store.scan_series(BINANCE, "talib_x").collect()
    assert back.schema == pl.Schema(
        {
            "open_time_ms": pl.Int64,
            "talib_x_real": pl.Float32,
            "talib_x_pattern": pl.Int16,
            "talib_x_index": pl.Int64,
        }
    )
    assert back["talib_x_pattern"].to_list()[:2] == [200, -200]
    assert {dtype_name(t) for t in dtypes.values()} == {"Float32", "Int16", "Int64"}


def test_int16_out_of_range_is_error_never_truncated(
    settings: FinancesSettings, times: list[int]
) -> None:
    store = IndicatorStore.from_settings(settings, "ft_ta_lib")
    df = pl.DataFrame({"open_time_ms": times, "talib_p": [40_000] * len(times)})
    with pytest.raises(SeriesDtypeError, match="Int16"):
        store.write_series(BINANCE, "talib_p", df, {"talib_p": pl.Int16})
    df = pl.DataFrame({"open_time_ms": times, "talib_p": [0.5] * len(times)})
    with pytest.raises(SeriesDtypeError, match="enteros"):
        store.write_series(BINANCE, "talib_p", df, {"talib_p": pl.Int16})
    assert not store.series_files(BINANCE, "talib_p")


def test_float64_is_rejected_at_persistence(store: IndicatorStore, times: list[int]) -> None:
    with pytest.raises(SeriesDtypeError, match="Float64"):
        store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), {"rsi_14": pl.Float64})


def test_namespace_enforced_on_series_and_columns(store: IndicatorStore, times: list[int]) -> None:
    with pytest.raises(NamespaceViolationError):
        store.write_series(
            BINANCE,
            "talib_rsi_14",
            series_frame(times, "talib_rsi_14"),
            {"talib_rsi_14": pl.Float32},
        )
    with pytest.raises(NamespaceViolationError):
        store.write_series(
            BINANCE, "rsi_14", series_frame(times, "talib_rsi_14"), {"talib_rsi_14": pl.Float32}
        )


def test_missing_output_column(store: IndicatorStore, times: list[int]) -> None:
    with pytest.raises(ValueError, match="rsi_14"):
        store.write_series(BINANCE, "rsi_14", series_frame(times, "other"), F32)


def test_nothing_written_under_parquet_dir(
    store: IndicatorStore, settings: FinancesSettings, times: list[int]
) -> None:
    before = tree_digest(settings.PARQUET_DIR)
    store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    assert tree_digest(settings.PARQUET_DIR) == before
    assert not store.root.is_relative_to(settings.PARQUET_DIR)


def test_writing_other_series_keeps_existing_bytes(store: IndicatorStore, times: list[int]) -> None:
    store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    rsi_before = {k: v for k, v in tree_digest(store.root).items() if "rsi_14" in k}
    store.write_series(BINANCE, "ema_50", series_frame(times, "ema_50"), {"ema_50": pl.Float32})
    after = tree_digest(store.root)
    assert {k: v for k, v in after.items() if "rsi_14" in k} == rsi_before
    assert any("ema_50" in k for k in after)


def test_two_namespaces_coexist_in_same_month(settings: FinancesSettings, times: list[int]) -> None:
    ind = IndicatorStore.from_settings(settings, "ft_indicators")
    tal = IndicatorStore.from_settings(settings, "ft_ta_lib")
    ind.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    tal.write_series(
        BINANCE,
        "talib_rsi_14",
        series_frame(times, "talib_rsi_14", offset=0.5),
        {"talib_rsi_14": pl.Float32},
    )
    month = ind.dataset_dir(BINANCE) / "year=2024" / "month=01"
    assert sorted(p.name for p in month.iterdir()) == ["rsi_14.parquet", "talib_rsi_14.parquet"]
    assert ind.scan_series(BINANCE, "rsi_14").collect()["rsi_14"][10] == 10.0
    assert tal.scan_series(BINANCE, "talib_rsi_14").collect()["talib_rsi_14"][10] == 10.5
    # rewrite talib does not touch ft_indicators file
    before = tree_digest(month / "..")
    tal.write_series(
        BINANCE, "talib_rsi_14", series_frame(times, "talib_rsi_14"), {"talib_rsi_14": pl.Float32}
    )
    after = tree_digest(month / "..")
    assert {k: v for k, v in after.items() if k.endswith("/rsi_14.parquet")} == {
        k: v for k, v in before.items() if k.endswith("/rsi_14.parquet")
    }


def test_month_without_source_rows_removes_file(store: IndicatorStore, times: list[int]) -> None:
    store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    store.write_series(BINANCE, "ema_50", series_frame(times, "ema_50"), {"ema_50": pl.Float32})
    stats = store.write_series(BINANCE, "rsi_14", series_frame(times[48:], "rsi_14"), F32)
    assert (stats.partitions_written, stats.partitions_removed) == (1, 1)
    assert not _month_file(store, 1, "rsi_14.parquet").exists()
    assert _month_file(store, 1, "ema_50.parquet").exists()  # otras series intactas
    assert store.scan_series(BINANCE, "rsi_14").collect().height == 52


def test_empty_frame_removes_all_files(store: IndicatorStore, times: list[int]) -> None:
    store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    stats = store.write_series(BINANCE, "rsi_14", series_frame([], "rsi_14"), F32)
    assert (stats.rows, stats.partitions_removed, stats.first_ms) == (0, 2, None)
    assert not store.has_series(BINANCE, "rsi_14")


def test_scan_series_range_and_missing(store: IndicatorStore, times: list[int]) -> None:
    store.write_series(BINANCE, "rsi_14", series_frame(times, "rsi_14"), F32)
    feb = store.scan_series(BINANCE, "rsi_14", start_ms=times[48], end_ms=times[50]).collect()
    assert feb["open_time_ms"].to_list() == times[48:50]  # end exclusivo
    empty = store.scan_series(BINANCE, "rsi_14", start_ms=times[-1] + 30 * 24 * HOUR_MS).collect()
    assert empty.height == 0
    assert empty.columns == ["open_time_ms", "rsi_14"]
    with pytest.raises(SeriesNotFoundError):
        store.scan_series(BINANCE, "ema_9")
    with pytest.raises(SeriesNotFoundError):
        store.scan_series(YFINANCE, "rsi_14")


def test_from_settings_validates_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_INDICATORS_DIR", "data/parquet/ind")
    with pytest.raises(ConfigError):
        IndicatorStore.from_settings(FinancesSettings(), "ft_indicators")
