"""T2.1–T2.5: analyzer (summary, gaps, anomalías, muestreo) sobre Parquet temporal real."""

from __future__ import annotations

import random
from pathlib import Path

import polars as pl
import pytest
from finances_core.schemas.data_viewer import ColumnStats, DatasetIdentifier

from finances_downloader.ft_data_viewer.analyzer import (
    compute_summary,
    detect_anomalies,
    detect_gaps,
    format_duration_human,
    reservoir_indices,
    sample_rows,
)
from finances_downloader.ft_data_viewer.scanner import read_dataset_metadata, scan_dataset

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv, write_dataset

IDENT = DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")


def _lazy(df: pl.DataFrame) -> pl.LazyFrame:
    return df.lazy()


# --- T2.1 compute_summary -------------------------------------------------------------


def test_compute_summary_clean_dataset(btc_dataset: Path) -> None:
    lf = scan_dataset(btc_dataset)
    s = compute_summary(lf, IDENT, metadata=read_dataset_metadata(btc_dataset))
    start = JAN_2024_MS + 30 * 24 * HOUR_MS
    assert s.dataset_id == IDENT
    assert s.row_count == 48
    assert s.date_range == (start, start + 47 * HOUR_MS)
    assert s.gaps_detected == 0
    assert s.duplicate_timestamps == 0
    assert s.outlier_count == 0
    by_name = {c.name: c for c in s.columns}
    assert "close" in by_name
    assert "open_time_ms" in by_name
    close = by_name["close"]
    df = scan_dataset(btc_dataset).collect()
    assert close.mean == pytest.approx(df["close"].mean(), rel=1e-5)
    assert close.std == pytest.approx(df["close"].std(), rel=1e-4)
    assert close.min == pytest.approx(df["close"].min())
    assert close.distinct_count == df["close"].n_unique()
    assert close.null_count == 0


def test_compute_summary_without_metadata_matches(btc_dataset: Path) -> None:
    lf = scan_dataset(btc_dataset)
    with_meta = compute_summary(lf, IDENT, metadata=read_dataset_metadata(btc_dataset))
    without = compute_summary(lf, IDENT)
    for a, b in zip(with_meta.columns, without.columns, strict=True):
        assert a.name == b.name
        assert a.min == pytest.approx(b.min)
        assert a.max == pytest.approx(b.max)
        assert a.null_count == b.null_count


def test_compute_summary_counts_problems() -> None:
    df = make_ohlcv(200, drop=[50, 51, 120])
    dup = df.filter(pl.col("open_time_ms").is_in(df["open_time_ms"].head(2).implode()))
    spikes = pl.int_range(pl.len()).is_in([10, 150])
    df = pl.concat([df, dup]).with_columns(
        pl.when(spikes).then(pl.lit(10_000.0, pl.Float32)).otherwise(pl.col("close")).alias("close")
    )
    s = compute_summary(_lazy(df), IDENT)
    assert s.gaps_detected == 2
    assert s.duplicate_timestamps == 2
    assert s.outlier_count == 2
    assert s.row_count == 199


def test_compute_summary_empty_frame() -> None:
    s = compute_summary(_lazy(make_ohlcv(0)), IDENT)
    assert s.row_count == 0
    assert s.date_range == (0, 0)
    assert s.gaps_detected == 0


# --- T2.2 detect_gaps -----------------------------------------------------------------


def test_detect_gaps_artificial() -> None:
    # Se eliminan las velas 5 (1 faltante) y 20..23 (4 faltantes).
    df = make_ohlcv(40, drop=[5, 20, 21, 22, 23])
    gaps = detect_gaps(_lazy(df), HOUR_MS)
    assert [(g.gap_start_ms, g.gap_end_ms, g.missing_candles) for g in gaps] == [
        (JAN_2024_MS + 5 * HOUR_MS, JAN_2024_MS + 5 * HOUR_MS, 1),
        (JAN_2024_MS + 20 * HOUR_MS, JAN_2024_MS + 23 * HOUR_MS, 4),
    ]
    assert gaps[0].gap_duration_human == "1h"
    assert gaps[1].gap_duration_human == "4h"


def test_detect_gaps_respects_min_gap_candles() -> None:
    df = make_ohlcv(40, drop=[5, 20, 21, 22, 23])
    gaps = detect_gaps(_lazy(df), HOUR_MS, min_gap_candles=2)
    assert [g.missing_candles for g in gaps] == [4]


def test_detect_gaps_respects_threshold_ms() -> None:
    df = make_ohlcv(40, drop=[5, 20, 21, 22, 23])
    gaps = detect_gaps(_lazy(df), HOUR_MS, gap_threshold_ms=2 * HOUR_MS)
    assert [g.missing_candles for g in gaps] == [4]


def test_detect_gaps_unsorted_input_and_duplicates() -> None:
    df = make_ohlcv(30, drop=[10, 11])
    shuffled = pl.concat([df, df.head(3)]).sample(fraction=1.0, shuffle=True, seed=1)
    gaps = detect_gaps(_lazy(shuffled), HOUR_MS)
    assert [(g.gap_start_ms, g.missing_candles) for g in gaps] == [(JAN_2024_MS + 10 * HOUR_MS, 2)]


def test_detect_gaps_none_and_tiny_inputs() -> None:
    assert detect_gaps(_lazy(make_ohlcv(30)), HOUR_MS) == []
    assert detect_gaps(_lazy(make_ohlcv(1)), HOUR_MS) == []
    assert detect_gaps(_lazy(make_ohlcv(0)), HOUR_MS) == []


def test_detect_gaps_invalid_args() -> None:
    with pytest.raises(ValueError, match="timeframe_ms"):
        detect_gaps(_lazy(make_ohlcv(3)), 0)
    with pytest.raises(ValueError, match="min_gap_candles"):
        detect_gaps(_lazy(make_ohlcv(3)), HOUR_MS, min_gap_candles=0)


def test_format_duration_human() -> None:
    assert format_duration_human(0) == "0s"
    assert format_duration_human(90_061_000) == "1d 1h 1m 1s"


# --- T2.3 detect_anomalies ------------------------------------------------------------


def _anomalous_frame() -> pl.DataFrame:
    """3 timestamps duplicados, 5 nulls en close, 2 outliers (z>3) en close, 10 volumen=0."""
    base = make_ohlcv(300)
    idx = pl.int_range(pl.len())
    df = base.with_columns(
        pl.when(idx.is_in([200, 250]))
        .then(pl.lit(10_000.0, pl.Float32))
        .when(idx.is_in([30, 31, 32, 33, 34]))
        .then(pl.lit(None, pl.Float32))
        .otherwise(pl.col("close"))
        .alias("close"),
        pl.when(idx.is_in(list(range(100, 110))))
        .then(pl.lit(0.0, pl.Float32))
        .otherwise(pl.col("volume"))
        .alias("volume"),
    )
    dups = df.slice(60, 3)  # 3 timestamps duplicados (sin nulls ni outliers)
    return pl.concat([df, dups])


def test_detect_anomalies_full_report() -> None:
    report = detect_anomalies(_lazy(_anomalous_frame()), IDENT)
    assert report.dataset_id == IDENT
    assert report.duplicate_timestamps["count"] == 3
    assert report.duplicate_timestamps["extra_rows"] == 3
    assert len(report.duplicate_timestamps["examples"]) == 3
    assert report.null_values == {"close": 5}
    assert set(report.price_outliers) == {"close"}
    assert len(report.price_outliers["close"]) == 2
    assert all(o["zscore"] > 3 for o in report.price_outliers["close"])
    zero = [a for a in report.volume_anomalies["volume"] if a["type"] == "zero_volume"]
    assert len(zero) == 10


def test_detect_anomalies_duplicate_count_not_capped() -> None:
    df = make_ohlcv(50)
    df = pl.concat([df, df.head(25)])
    report = detect_anomalies(_lazy(df), IDENT)
    assert report.duplicate_timestamps["count"] == 25
    assert len(report.duplicate_timestamps["examples"]) <= 10


def test_detect_anomalies_iqr_method() -> None:
    report = detect_anomalies(_lazy(_anomalous_frame()), IDENT, outlier_method="iqr")
    assert len(report.price_outliers["close"]) >= 2
    values = {o["value"] for o in report.price_outliers["close"]}
    assert 10_000.0 in values


def test_detect_anomalies_clean_dataset() -> None:
    report = detect_anomalies(_lazy(make_ohlcv(100)), IDENT)
    assert report.duplicate_timestamps["count"] == 0
    assert report.null_values == {}
    assert report.price_outliers == {}
    assert report.volume_anomalies == {}


def test_detect_anomalies_high_volume() -> None:
    idx = pl.int_range(pl.len())
    df = make_ohlcv(200).with_columns(
        pl.when(idx == 7).then(pl.lit(1e6, pl.Float32)).otherwise(pl.col("volume")).alias("volume")
    )
    report = detect_anomalies(_lazy(df), IDENT)
    (anomaly,) = report.volume_anomalies["volume"]
    assert anomaly["type"] == "high_volume"
    assert anomaly["zscore"] > 3


def test_detect_anomalies_rejects_invalid_method() -> None:
    with pytest.raises(ValueError, match="outlier_method"):
        detect_anomalies(_lazy(make_ohlcv(5)), IDENT, outlier_method="magic")


# --- T2.4 sample_rows -----------------------------------------------------------------


def test_head_and_tail_sorted(btc_dataset: Path) -> None:
    lf = scan_dataset(btc_dataset).sort("open_time_ms", descending=True)  # entrada desordenada
    head = sample_rows(lf, 3, "head")
    tail = sample_rows(lf, 3, "tail")
    ts = scan_dataset(btc_dataset).collect()["open_time_ms"].sort()
    assert head["open_time_ms"].to_list() == ts.head(3).to_list()
    assert tail["open_time_ms"].to_list() == ts.tail(3).reverse().to_list()


def test_head_projection_without_sort_column(btc_dataset: Path) -> None:
    out = sample_rows(scan_dataset(btc_dataset), 2, "head", cols=["close", "volume"])
    assert out.columns == ["close", "volume"]
    assert out.height == 2
    tail = sample_rows(scan_dataset(btc_dataset), 2, "tail", cols=["close"])
    assert tail.columns == ["close"]


def test_sample_reservoir_bounded_and_projected(btc_dataset: Path) -> None:
    out = sample_rows(scan_dataset(btc_dataset), 10, "sample", cols=["open_time_ms"], seed=7)
    assert out.height == 10
    assert out["open_time_ms"].n_unique() == 10
    again = sample_rows(scan_dataset(btc_dataset), 10, "sample", cols=["open_time_ms"], seed=7)
    assert out.equals(again)


def test_sample_n_larger_than_dataset_returns_all(btc_dataset: Path) -> None:
    assert sample_rows(scan_dataset(btc_dataset), 1000, "sample").height == 48


def test_sample_does_not_collect_full_frame(
    btc_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []
    original = pl.LazyFrame.collect

    def spy(self: pl.LazyFrame, *a: object, **k: object) -> pl.DataFrame:
        df = original(self, *a, **k)
        sizes.append(df.height)
        return df

    monkeypatch.setattr(pl.LazyFrame, "collect", spy)
    sample_rows(scan_dataset(btc_dataset), 5, "sample", seed=1)
    assert max(sizes) <= 5


def test_reservoir_indices_uniform_and_valid() -> None:
    rng = random.Random(0)
    idx = reservoir_indices(1000, 50, rng)
    assert len(idx) == len(set(idx)) == 50
    assert all(0 <= i < 1000 for i in idx)
    assert idx == sorted(idx)
    assert reservoir_indices(5, 10, rng) == [0, 1, 2, 3, 4]
    assert reservoir_indices(0, 3, rng) == []
    # Uniformidad aproximada: cada índice aparece ~k/N de las veces.
    counts = [0] * 20
    for _ in range(4000):
        for i in reservoir_indices(20, 5, rng):
            counts[i] += 1
    assert all(800 < c < 1200 for c in counts)


def test_sample_rows_invalid_args(btc_dataset: Path) -> None:
    with pytest.raises(ValueError, match="n"):
        sample_rows(scan_dataset(btc_dataset), 0, "head")
    with pytest.raises(ValueError, match="mode"):
        sample_rows(scan_dataset(btc_dataset), 1, "middle")


def test_analyzer_on_written_dataset_with_gaps(parquet_dir: Path) -> None:
    root = write_dataset(parquet_dir, make_ohlcv(100, drop=[40, 41, 42]))
    gaps = detect_gaps(scan_dataset(root), HOUR_MS)
    assert len(gaps) == 1
    assert gaps[0].missing_candles == 3
    assert isinstance(compute_summary(scan_dataset(root), IDENT).columns[0], ColumnStats)
