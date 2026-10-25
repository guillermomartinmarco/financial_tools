"""T1.10 (ft_indicators): ``build_wide_view`` (rango, series/columnas, OHLCV, stability, stale)."""

from __future__ import annotations

import polars as pl
import pytest
from indicators_helpers import BINANCE, HOUR_MS, YFINANCE, series_frame, write_source

from finances_core.config import FinancesSettings
from finances_core.indicators_store.store import IndicatorStore, SeriesNotFoundError
from finances_core.indicators_store.view import ExportValidationError, build_wide_view
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    EXPORT_ID_COLUMNS,
    SeriesExportRequest,
    Stability,
    StoreEntry,
)


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


def _entry(
    sid: str, columns: list[str], times: list[int], stable_idx: int | None, dtype: str = "Float32"
) -> StoreEntry:
    stable = times[stable_idx] if stable_idx is not None else None
    return StoreEntry(
        series_id=sid,
        spec={},
        columns=columns,
        column_dtypes=dict.fromkeys(columns, dtype),
        engine_version="test:1",
        rows=len(times),
        first_ms=times[0],
        last_ms=times[-1],
        stability=Stability(
            stable_from_ms=stable,
            by_column=dict.fromkeys(columns, stable),
            rule="",
            reason="converged" if stable is not None else "non_decaying_seed",
        ),
        computed_at_ms=0,
    )


def _compute(store: IndicatorStore, ds: DatasetIdentifier, times: list[int]) -> None:
    """``rsi_14`` (estable desde 10), ``ema_50`` (desde 20) y ``obv`` (sin estabilidad)."""
    entries = {}
    for sid, stable in (("rsi_14", 10), ("ema_50", 20), ("obv", None)):
        store.write_series(ds, sid, series_frame(times, sid), {sid: pl.Float32})
        entries[sid] = _entry(sid, [sid], times, stable)
    store.update_manifest(ds, entries)


@pytest.fixture
def ind(settings: FinancesSettings) -> IndicatorStore:
    return IndicatorStore.from_settings(settings, "ft_indicators")


@pytest.fixture
def times(settings: FinancesSettings, ind: IndicatorStore) -> list[int]:
    t = write_source(settings.PARQUET_DIR, BINANCE)
    _compute(ind, BINANCE, t)
    return t


def _view(
    ind: IndicatorStore, settings: FinancesSettings, ds: DatasetIdentifier = BINANCE, **kw: object
) -> pl.DataFrame:
    view = build_wide_view(ind, ds, SeriesExportRequest(**kw), settings=settings)
    return view.frame.collect()


def test_wide_table_with_identity_columns(
    ind: IndicatorStore, settings: FinancesSettings, times: list[int]
) -> None:
    df = _view(ind, settings)
    assert df.columns == [*EXPORT_ID_COLUMNS, "ema_50", "obv", "rsi_14"]
    assert df.height == 100
    assert df["open_time_ms"].to_list() == times
    assert df["exchange"].unique().to_list() == ["binance"]
    assert df["symbol"].unique().to_list() == ["BTCUSDT"]
    assert df.schema["rsi_14"] == pl.Float32
    assert df["rsi_14"].null_count() == 3


def test_range_end_exclusive_and_series_filter(
    ind: IndicatorStore, settings: FinancesSettings, times: list[int]
) -> None:
    df = _view(ind, settings, start_ms=times[40], end_ms=times[60], series=["rsi_14"])
    assert df["open_time_ms"].to_list() == times[40:60]
    assert df.columns == [*EXPORT_ID_COLUMNS, "rsi_14"]


def test_columns_filter(ind: IndicatorStore, settings: FinancesSettings, times: list[int]) -> None:
    df = _view(ind, settings, columns=["ema_50"])
    assert df.columns == [*EXPORT_ID_COLUMNS, "ema_50"]


def test_unknown_series_or_column_is_not_found(
    ind: IndicatorStore, settings: FinancesSettings, times: list[int]
) -> None:
    with pytest.raises(SeriesNotFoundError, match="sma_9"):
        _view(ind, settings, series=["sma_9"])
    with pytest.raises(SeriesNotFoundError, match="macd_x"):
        _view(ind, settings, columns=["macd_x"])
    with pytest.raises(SeriesNotFoundError):
        _view(ind, settings, YFINANCE)


def test_include_ohlcv_binance_has_no_adj_close(
    ind: IndicatorStore, settings: FinancesSettings, times: list[int]
) -> None:
    df = _view(ind, settings, include_ohlcv=True, series=["rsi_14"])
    assert df.columns == [*EXPORT_ID_COLUMNS, "open", "high", "low", "close", "volume", "rsi_14"]
    assert df.schema["close"] == pl.Float32


def test_include_ohlcv_yfinance_has_adj_close(
    ind: IndicatorStore, settings: FinancesSettings
) -> None:
    t = write_source(settings.PARQUET_DIR, YFINANCE)
    _compute(ind, YFINANCE, t)
    df = _view(ind, settings, YFINANCE, include_ohlcv=True, series=["rsi_14"])
    assert df.columns == [
        *EXPORT_ID_COLUMNS,
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "rsi_14",
    ]


def test_stability_flag(ind: IndicatorStore, settings: FinancesSettings, times: list[int]) -> None:
    df = _view(ind, settings, stability="flag")
    assert df["rsi_14_is_stable"].to_list() == [False] * 10 + [True] * 90
    assert df["ema_50_is_stable"].sum() == 80
    assert df["obv_is_stable"].sum() == 0
    assert df.schema["rsi_14_is_stable"] == pl.Boolean


def test_stability_drop_uses_max_and_rejects_null(
    ind: IndicatorStore, settings: FinancesSettings, times: list[int]
) -> None:
    df = _view(ind, settings, stability="drop", series=["rsi_14", "ema_50"])
    assert df["open_time_ms"].to_list() == times[20:]
    with pytest.raises(ExportValidationError, match="obv"):
        _view(ind, settings, stability="drop")


def test_invalid_range(ind: IndicatorStore, settings: FinancesSettings, times: list[int]) -> None:
    with pytest.raises(ExportValidationError, match="start_ms"):
        _view(ind, settings, start_ms=times[10], end_ms=times[5])


def test_stale_detected(ind: IndicatorStore, settings: FinancesSettings, times: list[int]) -> None:
    assert build_wide_view(ind, BINANCE, SeriesExportRequest(), settings=settings).stale == []
    write_source(settings.PARQUET_DIR, BINANCE, rows=5, start_ms=times[-1] + HOUR_MS)
    view = build_wide_view(ind, BINANCE, SeriesExportRequest(), settings=settings)
    assert view.stale == ["ema_50", "obv", "rsi_14"]
    assert view.frame.collect().height == 100  # se exporta lo persistido


def test_int_columns_preserved_and_namespaces_isolated(
    ind: IndicatorStore, settings: FinancesSettings, times: list[int]
) -> None:
    tal = IndicatorStore.from_settings(settings, "ft_ta_lib")
    n = len(times)
    df = pl.DataFrame(
        {"open_time_ms": times, "talib_cdldoji": [100 if i % 3 else 0 for i in range(n)]}
    )
    tal.write_series(BINANCE, "talib_cdldoji", df, {"talib_cdldoji": pl.Int16})
    idx = pl.DataFrame({"open_time_ms": times, "talib_maxindex_30": times})
    tal.write_series(BINANCE, "talib_maxindex_30", idx, {"talib_maxindex_30": pl.Int64})
    tal.update_manifest(
        BINANCE,
        {
            "talib_cdldoji": _entry("talib_cdldoji", ["talib_cdldoji"], times, 0, "Int16"),
            "talib_maxindex_30": _entry(
                "talib_maxindex_30", ["talib_maxindex_30"], times, 0, "Int64"
            ),
        },
    )
    out = build_wide_view(tal, BINANCE, SeriesExportRequest(), settings=settings).frame.collect()
    assert out.schema["talib_cdldoji"] == pl.Int16
    assert out.schema["talib_maxindex_30"] == pl.Int64
    assert "rsi_14" not in out.columns
    own = _view(ind, settings)
    assert not any(c.startswith("talib_") for c in own.columns)
