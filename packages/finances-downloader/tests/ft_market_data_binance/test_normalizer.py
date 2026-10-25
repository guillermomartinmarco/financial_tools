"""T3.1 — Normalización canónica (RF-3)."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from finances_core.schemas.market_data import OHLCV_SCHEMA

from finances_downloader.ft_market_data_binance.normalizer import (
    normalize_binance_klines,
    normalize_rest_json,
    read_public_data_csv,
    read_public_data_zip,
)

from .conftest import csv_bytes, kline_row, ms, zip_bytes

H = 3_600_000
CANONICAL = list(OHLCV_SCHEMA)


def _assert_canonical(df: pl.DataFrame) -> None:
    assert df.columns == [*CANONICAL, "year", "month"]
    for col, dtype in OHLCV_SCHEMA.items():
        assert df.schema[col] == dtype, col
    assert df["open_time_ms"].is_sorted()
    assert df["open_time_ms"].n_unique() == df.height


def test_rest_json_to_canonical_schema() -> None:
    start = ms(2024, 1, 31, 23)
    rows = [kline_row(start + i * H) for i in (1, 0, 1)]  # desordenado y duplicado
    df = normalize_rest_json(rows, "BTCUSDT", "1h", "spot", start, start + 10 * H)
    _assert_canonical(df)
    assert df["open_time_ms"].to_list() == [start, start + H]
    assert df["symbol"].to_list() == ["BTCUSDT", "BTCUSDT"]
    assert df["market_type"].unique().to_list() == ["spot"]
    assert df["year"].to_list() == [2024, 2024]
    assert df["month"].to_list() == [1, 2]
    assert df["open"][0] == pytest.approx(100.0)
    assert df["trades_count"][0] == 7


def test_rows_outside_range_are_filtered() -> None:
    start = ms(2024, 1, 1)
    rows = [kline_row(start + i * H) for i in range(10)]
    df = normalize_rest_json(rows, "BTCUSDT", "1h", "spot", start + 2 * H, start + 4 * H)
    assert df["open_time_ms"].to_list() == [start + 2 * H, start + 3 * H, start + 4 * H]


def test_headerless_spot_csv_keeps_first_row(tmp_path: Path) -> None:
    """Bug #10: ``has_header=True`` descartaba la primera vela de los CSV spot sin cabecera."""
    start = ms(2024, 1, 1)
    csv = tmp_path / "spot.csv"
    csv.write_bytes(csv_bytes([start, start + H, start + 2 * H]))
    df = read_public_data_csv(str(csv), "BTCUSDT", "1h", "spot", start, start + 3 * H)
    _assert_canonical(df)
    assert df["open_time_ms"].to_list() == [start, start + H, start + 2 * H]


def test_futures_csv_with_header(tmp_path: Path) -> None:
    start = ms(2024, 1, 1)
    csv = tmp_path / "fut.csv"
    csv.write_bytes(csv_bytes([start, start + H], header=True))
    df = read_public_data_csv(str(csv), "ETHUSDT", "1h", "futures", start, start + 3 * H)
    assert df["open_time_ms"].to_list() == [start, start + H]


def test_microsecond_timestamps_normalized_to_ms(tmp_path: Path) -> None:
    """Desde 2025 Binance Spot Public Data publica timestamps en microsegundos."""
    start = ms(2025, 1, 1)
    csv = tmp_path / "us.csv"
    csv.write_bytes(csv_bytes([start, start + H], microseconds=True))
    df = read_public_data_csv(str(csv), "BTCUSDT", "1h", "spot", start, start + 3 * H)
    assert df["open_time_ms"].to_list() == [start, start + H]
    assert df.schema["open_time_ms"] == pl.Int64


def test_read_public_data_zip(tmp_path: Path) -> None:
    start = ms(2024, 1, 1)
    z = tmp_path / "x.zip"
    z.write_bytes(zip_bytes("BTCUSDT-1h-2024-01.csv", csv_bytes([start, start + H])))
    df = read_public_data_zip(z, "BTCUSDT", "1h", "spot", start, start + 5 * H)
    _assert_canonical(df)
    assert df.height == 2


def test_read_public_data_zip_without_csv_raises(tmp_path: Path) -> None:
    z = tmp_path / "empty.zip"
    z.write_bytes(zip_bytes("readme.txt", b"hi"))
    with pytest.raises(ValueError, match="CSV"):
        read_public_data_zip(z, "BTCUSDT", "1h", "spot", 0, 1)


def test_normalize_binance_klines_rejects_missing_columns() -> None:
    df = pl.DataFrame({"open_time": ["1"]})
    with pytest.raises(ValueError, match="columnas"):
        normalize_binance_klines(
            df,
            "rest_api",
            symbol="BTCUSDT",
            timeframe="1h",
            market_type="spot",
            start_ms=0,
            end_ms=2,
        )


def test_empty_input_gives_empty_canonical_frame() -> None:
    df = normalize_rest_json([], "BTCUSDT", "1h", "spot", 0, 10)
    assert df.height == 0
    assert df.columns == [*CANONICAL, "year", "month"]
