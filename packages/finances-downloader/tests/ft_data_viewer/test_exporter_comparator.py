"""T3.1–T3.3: exportación streaming y comparación de datasets."""

from __future__ import annotations

import io
import json
from pathlib import Path

import polars as pl
import pytest
from finances_core.schemas.data_viewer import DatasetIdentifier, GapInfo

from finances_downloader.ft_data_viewer.comparator import compare_datasets, subtract_gaps
from finances_downloader.ft_data_viewer.exporter import stream_export

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv

A = DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")
B = DatasetIdentifier(exchange="binance", market_type="futures", symbol="BTCUSDT", timeframe="1h")


# --- T3.1 stream_export ---------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_stream_export_to_path(tmp_path: Path, fmt: str) -> None:
    df = make_ohlcv(25)
    out = tmp_path / "out" / f"x.{fmt}"
    rows = stream_export(df.lazy(), fmt, out)
    assert rows == 25
    if fmt == "parquet":
        back = pl.read_parquet(out)
    elif fmt == "csv":
        back = pl.read_csv(out)
    else:
        back = pl.read_ndjson(out)
    assert back.height == 25
    assert back["open_time_ms"].to_list() == df["open_time_ms"].to_list()


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_stream_export_to_binary_stream(fmt: str) -> None:
    buf = io.BytesIO()
    rows = stream_export(make_ohlcv(7).lazy(), fmt, buf)
    assert rows == 7
    data = buf.getvalue()
    if fmt == "parquet":
        assert data[:4] == b"PAR1"
    elif fmt == "csv":
        assert data.decode().splitlines()[0].startswith("open_time_ms,")
        assert len(data.decode().splitlines()) == 8
    else:
        lines = data.decode().splitlines()
        assert len(lines) == 7
        assert "close" in json.loads(lines[0])


def test_stream_export_uses_sink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: list[str] = []
    original = pl.LazyFrame.sink_parquet

    def spy(self: pl.LazyFrame, *a: object, **k: object) -> object:
        called.append("sink_parquet")
        return original(self, *a, **k)

    monkeypatch.setattr(pl.LazyFrame, "sink_parquet", spy)
    stream_export(make_ohlcv(3).lazy(), "parquet", tmp_path / "a.parquet")
    assert called == ["sink_parquet"]


def test_stream_export_rejects_unknown_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Formato"):
        stream_export(make_ohlcv(3).lazy(), "xlsx", tmp_path / "a")


def test_stream_export_large_simulated_dataset(tmp_path: Path) -> None:
    n = 500_000
    lf = pl.LazyFrame({"open_time_ms": pl.int_range(0, n, eager=True)}).with_columns(
        pl.col("open_time_ms").cast(pl.Float32).alias("close")
    )
    assert stream_export(lf, "parquet", tmp_path / "big.parquet") == n


# --- T3.2 compare_datasets ------------------------------------------------------------


def test_compare_identical_except_five_closes() -> None:
    a = make_ohlcv(100)
    idx = pl.int_range(pl.len())
    b = a.with_columns(
        pl.when(idx.is_in([3, 10, 20, 30]))
        .then(pl.col("close") + pl.lit(2.5, pl.Float32))
        .when(idx == 50)
        .then(pl.col("close") + pl.lit(7.0, pl.Float32))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    # 5 filas divergentes: 4 con +2.5 y una (la máxima) con +7.0.
    res = compare_datasets(a.lazy(), b.lazy(), A, B)
    assert res.price_divergence_max == pytest.approx(7.0, abs=1e-3)
    assert res.gaps_only_in_a == []
    assert res.gaps_only_in_b == []
    assert res.date_range_overlap == (JAN_2024_MS, JAN_2024_MS + 99 * HOUR_MS)
    assert res.row_count_diff == 0
    assert res.volume_divergence_pct == 0.0


def test_compare_gaps_only_in_each_side() -> None:
    a = make_ohlcv(60, drop=[10, 11, 12, 40])
    b = make_ohlcv(60, drop=[11, 30])
    res = compare_datasets(a.lazy(), b.lazy(), A, B)
    assert [(g.gap_start_ms, g.gap_end_ms, g.missing_candles) for g in res.gaps_only_in_a] == [
        (JAN_2024_MS + 10 * HOUR_MS, JAN_2024_MS + 10 * HOUR_MS, 1),
        (JAN_2024_MS + 12 * HOUR_MS, JAN_2024_MS + 12 * HOUR_MS, 1),
        (JAN_2024_MS + 40 * HOUR_MS, JAN_2024_MS + 40 * HOUR_MS, 1),
    ]
    assert [(g.gap_start_ms, g.missing_candles) for g in res.gaps_only_in_b] == [
        (JAN_2024_MS + 30 * HOUR_MS, 1)
    ]
    assert res.row_count_diff == -2


def test_compare_gaps_outside_overlap_are_ignored() -> None:
    a = make_ohlcv(50, drop=[5])
    b = make_ohlcv(30, start_ms=JAN_2024_MS + 20 * HOUR_MS)
    res = compare_datasets(a.lazy(), b.lazy(), A, B)
    assert res.date_range_overlap == (JAN_2024_MS + 20 * HOUR_MS, JAN_2024_MS + 49 * HOUR_MS)
    assert res.gaps_only_in_a == []


def test_compare_range_filter_and_no_overlap() -> None:
    a = make_ohlcv(10)
    b = make_ohlcv(10, start_ms=JAN_2024_MS + 100 * HOUR_MS)
    res = compare_datasets(a.lazy(), b.lazy(), A, B)
    assert res.date_range_overlap is None
    assert res.price_divergence_max == 0.0
    window = pl.col("open_time_ms").is_between(JAN_2024_MS + 2 * HOUR_MS, JAN_2024_MS + 4 * HOUR_MS)
    filtered = compare_datasets(a.lazy().filter(window), a.lazy().filter(window), A, A)
    assert filtered.date_range_overlap == (JAN_2024_MS + 2 * HOUR_MS, JAN_2024_MS + 4 * HOUR_MS)


def test_compare_volume_divergence() -> None:
    a = make_ohlcv(10)
    b = a.with_columns((pl.col("volume") * 2).alias("volume"))
    res = compare_datasets(a.lazy(), b.lazy(), A, B)
    assert res.volume_divergence_pct == pytest.approx(100.0, rel=1e-4)


def test_compare_empty_side() -> None:
    res = compare_datasets(make_ohlcv(5).lazy(), make_ohlcv(0).lazy(), A, B)
    assert res.row_count_diff == 5
    assert res.date_range_overlap is None
    assert res.volume_divergence_pct == 0.0


def test_subtract_gaps_partial_overlap() -> None:
    gap = GapInfo(
        gap_start_ms=10 * HOUR_MS,
        gap_end_ms=15 * HOUR_MS,
        missing_candles=6,
        gap_duration_human="6h",
    )
    other = GapInfo(
        gap_start_ms=12 * HOUR_MS,
        gap_end_ms=13 * HOUR_MS,
        missing_candles=2,
        gap_duration_human="2h",
    )
    pieces = subtract_gaps([gap], [other], HOUR_MS, (0, 100 * HOUR_MS))
    assert [
        (p.gap_start_ms // HOUR_MS, p.gap_end_ms // HOUR_MS, p.missing_candles) for p in pieces
    ] == [
        (10, 11, 2),
        (14, 15, 2),
    ]
    assert pieces[0].gap_duration_human == "2h"
