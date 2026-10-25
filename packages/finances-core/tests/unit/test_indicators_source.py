"""T1.7 (ft_indicators): carga y validación (Art. 13) de la fuente Hive."""

from __future__ import annotations

import math

import polars as pl
import pytest
from indicators_helpers import BINANCE, YFINANCE, write_source

from finances_core.config import FinancesSettings
from finances_core.indicators_store.source import (
    InvalidSourceError,
    SourceNotFoundError,
    available_columns,
    dataset_dir,
    dataset_exists,
    load_source,
    validate_source,
)
from finances_core.schemas.data_viewer import DatasetIdentifier


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


def test_dataset_dir_and_exists(settings: FinancesSettings) -> None:
    assert not dataset_exists(settings, BINANCE)
    write_source(settings.PARQUET_DIR, BINANCE)
    assert dataset_exists(settings, BINANCE)
    assert dataset_dir(settings, BINANCE) == (
        settings.PARQUET_DIR / "binance" / "market_type=spot" / "symbol=BTCUSDT" / "timeframe=1h"
    )
    other = BINANCE.model_copy(update={"timeframe": "4h"})
    assert not dataset_exists(settings, other)


def test_available_columns_binance_and_yfinance(settings: FinancesSettings) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)
    write_source(settings.PARQUET_DIR, YFINANCE)
    cols_b = available_columns(settings, BINANCE)
    cols_y = available_columns(settings, YFINANCE)
    assert {"open_time_ms", "open", "high", "low", "close", "volume"} <= cols_b
    assert "adj_close" not in cols_b
    assert "adj_close" in cols_y


def test_available_columns_reads_only_schema(
    settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("no debe leer datos")

    monkeypatch.setattr(pl, "read_parquet", _boom)
    monkeypatch.setattr(pl, "scan_parquet", _boom)
    assert "close" in available_columns(settings, BINANCE)


def test_available_columns_missing_dataset(settings: FinancesSettings) -> None:
    with pytest.raises(SourceNotFoundError):
        available_columns(settings, BINANCE)


@pytest.mark.parametrize("ds", [BINANCE, YFINANCE])
def test_load_source_projects_only_requested_columns(
    settings: FinancesSettings, ds: DatasetIdentifier
) -> None:
    times = write_source(settings.PARQUET_DIR, ds)  # cruza enero -> febrero
    src = load_source(settings, ds, ["close", "high"])
    assert src.frame.columns == ["open_time_ms", "close", "high"]
    assert src.frame["open_time_ms"].to_list() == times
    assert src.dataset == ds
    assert src.frame.schema["close"] == pl.Float32


def test_load_source_adj_close_only_yfinance(settings: FinancesSettings) -> None:
    write_source(settings.PARQUET_DIR, BINANCE)
    write_source(settings.PARQUET_DIR, YFINANCE)
    assert "adj_close" in load_source(settings, YFINANCE, ["adj_close"]).frame.columns
    with pytest.raises(InvalidSourceError, match="adj_close"):
        load_source(settings, BINANCE, ["adj_close"])


def test_load_source_missing_dataset(settings: FinancesSettings) -> None:
    with pytest.raises(SourceNotFoundError):
        load_source(settings, BINANCE, ["close"])


# --- validate_source --------------------------------------------------------------------


def _frame(times: list[int], closes: list[float | None]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series(times, dtype=pl.Int64),
            "close": pl.Series(closes, dtype=pl.Float32),
            "volume": pl.Series([1.0] * len(times), dtype=pl.Float32),
        }
    )


def test_validate_converts_non_finite_to_null_with_counts() -> None:
    df = _frame([1, 2, 3, 4, 5], [1.0, math.nan, math.inf, None, -math.inf])
    checked = validate_source(df, ["close", "volume"])
    assert checked.frame["close"].to_list() == [1.0, None, None, None, None]
    assert checked.null_counts == {"close": 4, "volume": 0}
    assert checked.frame.schema["close"] == pl.Float32


def test_validate_clean_source_passes() -> None:
    checked = validate_source(_frame([1, 2, 3], [1.0, 2.0, 3.0]), ["close"])
    assert checked.null_counts == {"close": 0}
    assert checked.frame.height == 3


def test_validate_missing_column() -> None:
    with pytest.raises(InvalidSourceError, match="high"):
        validate_source(_frame([1, 2], [1.0, 2.0]), ["close", "high"])


def test_validate_non_canonical_dtype() -> None:
    df = _frame([1, 2], [1.0, 2.0]).with_columns(pl.col("close").cast(pl.Float64))
    with pytest.raises(InvalidSourceError, match="Float32"):
        validate_source(df, ["close"])
    df = _frame([1, 2], [1.0, 2.0]).with_columns(pl.col("open_time_ms").cast(pl.Int32))
    with pytest.raises(InvalidSourceError, match="Int64"):
        validate_source(df, ["close"])


@pytest.mark.parametrize("times", [[1, 1, 2], [1, 3, 2], [3, 2, 1]])
def test_validate_open_time_not_strictly_increasing(times: list[int]) -> None:
    with pytest.raises(InvalidSourceError, match="open_time_ms"):
        validate_source(_frame(times, [1.0, 2.0, 3.0]), ["close"])


def test_validate_null_open_time() -> None:
    df = _frame([1, 2, 3], [1.0, 2.0, 3.0]).with_columns(
        pl.when(pl.col("open_time_ms") == 2)
        .then(None)
        .otherwise(pl.col("open_time_ms"))
        .alias("open_time_ms")
    )
    with pytest.raises(InvalidSourceError, match="open_time_ms"):
        validate_source(df, ["close"])


def test_invalid_source_error_code() -> None:
    err = InvalidSourceError("x")
    assert err.code == "invalid_source"
