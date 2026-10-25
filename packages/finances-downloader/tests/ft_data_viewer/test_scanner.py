"""T1.1–T1.4: scanner (layout Hive RF-4, metadata-only, resolución de rutas)."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from finances_core.schemas.data_viewer import DatasetIdentifier, DatasetInfo

from finances_downloader.ft_data_viewer import scanner
from finances_downloader.ft_data_viewer.errors import DatasetNotFoundError
from finances_downloader.ft_data_viewer.scanner import (
    dataset_files,
    read_dataset_metadata,
    read_row_count,
    resolve_dataset_path,
    scan_dataset,
    scan_parquet_partitions,
)

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv, write_dataset


@pytest.fixture
def multi(parquet_dir: Path) -> Path:
    """Varios exchanges, mercados, símbolos y timeframes."""
    write_dataset(parquet_dir, make_ohlcv(48, start_ms=JAN_2024_MS + 30 * 24 * HOUR_MS))
    write_dataset(
        parquet_dir,
        make_ohlcv(10, tf_ms=300_000),
        market_type="futures",
        symbol="ETHUSDT",
        timeframe="5m",
    )
    write_dataset(parquet_dir, make_ohlcv(5), symbol="ETHUSDT")
    write_dataset(parquet_dir, make_ohlcv(3), exchange="bybit", symbol="SOLUSDT")
    return parquet_dir


def _ids(infos: list[DatasetInfo]) -> set[tuple[str, str, str, str]]:
    return {(d.id.exchange, d.id.market_type, d.id.symbol, d.id.timeframe) for d in infos}


def test_scan_parses_full_hive_layout(multi: Path) -> None:
    infos = scan_parquet_partitions(multi)
    assert _ids(infos) == {
        ("binance", "spot", "BTCUSDT", "1h"),
        ("binance", "futures", "ETHUSDT", "5m"),
        ("binance", "spot", "ETHUSDT", "1h"),
        ("bybit", "spot", "SOLUSDT", "1h"),
    }
    btc = next(d for d in infos if d.id.symbol == "BTCUSDT")
    assert btc.year_range == (2024, 2024)
    assert btc.month_count == 2  # enero + febrero
    assert btc.estimated_rows == 48
    files = list((multi / btc.partition_path).rglob("*.parquet"))
    assert btc.size_bytes == sum(f.stat().st_size for f in files)
    assert btc.last_modified_ms > 0
    # Ruta relativa en formato POSIX (portable en JSON entre SO).
    assert btc.partition_path == "binance/market_type=spot/symbol=BTCUSDT/timeframe=1h"


def test_scan_year_range_spans_years(parquet_dir: Path) -> None:
    dec_2023 = JAN_2024_MS - 24 * HOUR_MS
    write_dataset(parquet_dir, make_ohlcv(48, start_ms=dec_2023))
    (info,) = scan_parquet_partitions(parquet_dir)
    assert info.year_range == (2023, 2024)
    assert info.month_count == 2


def test_scan_empty_or_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert scan_parquet_partitions(tmp_path / "nope") == []
    (tmp_path / "empty").mkdir()
    assert scan_parquet_partitions(tmp_path / "empty") == []


def test_scan_skips_invalid_market_type_and_non_hive_dirs(parquet_dir: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(3), market_type="options")
    (parquet_dir / "binance" / "garbage").mkdir(parents=True)
    write_dataset(parquet_dir, make_ohlcv(3))
    assert _ids(scan_parquet_partitions(parquet_dir)) == {("binance", "spot", "BTCUSDT", "1h")}


def test_scan_reports_progress(multi: Path) -> None:
    calls: list[tuple[int, int]] = []
    scan_parquet_partitions(multi, on_progress=lambda done, total: calls.append((done, total)))
    assert calls
    assert calls[-1][0] == calls[-1][1] == 4


def test_resolve_dataset_path_binance_spot_btc_1h(btc_dataset: Path, parquet_dir: Path) -> None:
    ident = DatasetIdentifier(
        exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h"
    )
    path = resolve_dataset_path(parquet_dir, ident)
    assert path == btc_dataset
    assert path == parquet_dir / "binance" / "market_type=spot" / "symbol=BTCUSDT" / "timeframe=1h"


@pytest.mark.parametrize(
    "ident",
    [
        DatasetIdentifier(exchange="binance", market_type="spot", symbol="NOPE", timeframe="1h"),
        DatasetIdentifier(
            exchange="binance", market_type="futures", symbol="BTCUSDT", timeframe="1h"
        ),
        DatasetIdentifier(exchange="kraken", market_type="spot", symbol="BTCUSDT", timeframe="1h"),
        DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="4h"),
    ],
)
def test_resolve_dataset_path_not_found_raises(
    btc_dataset: Path, parquet_dir: Path, ident: DatasetIdentifier
) -> None:
    with pytest.raises(DatasetNotFoundError):
        resolve_dataset_path(parquet_dir, ident)


def test_resolve_dataset_path_dir_without_parquet_raises(parquet_dir: Path) -> None:
    (parquet_dir / "binance" / "market_type=spot" / "symbol=X" / "timeframe=1h").mkdir(parents=True)
    ident = DatasetIdentifier(market_type="spot", symbol="X", timeframe="1h")
    with pytest.raises(DatasetNotFoundError):
        resolve_dataset_path(parquet_dir, ident)


def test_read_dataset_metadata_uses_parquet_statistics(btc_dataset: Path) -> None:
    meta = read_dataset_metadata(btc_dataset)
    df = pl.read_parquet(dataset_files(btc_dataset))
    assert set(meta) == set(df.columns)
    close = meta["close"]
    assert close.name == "close"
    assert close.dtype == "Float32"
    assert close.min == pytest.approx(df["close"].min())
    assert close.max == pytest.approx(df["close"].max())
    assert close.null_count == 0
    ts = meta["open_time_ms"]
    assert ts.min == df["open_time_ms"].min()
    assert ts.max == df["open_time_ms"].max()
    # La media no está en las estadísticas Parquet: no se inventa (metadata-only).
    assert close.mean is None


def test_read_dataset_metadata_counts_nulls(parquet_dir: Path) -> None:
    df = make_ohlcv(10).with_columns(
        pl.when(pl.int_range(pl.len()) < 3).then(None).otherwise(pl.col("close")).alias("close")
    )
    root = write_dataset(parquet_dir, df)
    assert read_dataset_metadata(root)["close"].null_count == 3


def test_read_dataset_metadata_does_not_read_rows(
    btc_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("no debe leer filas")

    monkeypatch.setattr(pl, "read_parquet", _boom)
    monkeypatch.setattr(pl.LazyFrame, "collect", _boom)
    assert read_dataset_metadata(btc_dataset)["close"].max is not None
    assert read_row_count(btc_dataset) == 48


def test_scan_dataset_has_no_partition_columns(btc_dataset: Path) -> None:
    lf = scan_dataset(btc_dataset)
    cols = lf.collect_schema().names()
    assert "year" not in cols
    assert "month" not in cols
    assert lf.select(pl.len()).collect().item() == 48


def test_scan_skips_timeframe_without_files_and_unreadable(
    parquet_dir: Path, btc_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (parquet_dir / "binance" / "market_type=spot" / "symbol=EMPTY" / "timeframe=1h").mkdir(
        parents=True
    )
    assert _ids(scan_parquet_partitions(parquet_dir)) == {("binance", "spot", "BTCUSDT", "1h")}

    def broken(_path: Path) -> object:
        raise OSError("footer corrupto")

    monkeypatch.setattr(scanner, "_file_metadata", broken)
    assert scan_parquet_partitions(parquet_dir) == []


def test_read_dataset_metadata_without_statistics(parquet_dir: Path) -> None:
    root = parquet_dir / "binance" / "market_type=spot" / "symbol=X" / "timeframe=1h"
    target = root / "year=2024" / "month=01"
    target.mkdir(parents=True)
    make_ohlcv(5).write_parquet(target / "data.parquet", statistics=False)
    meta = read_dataset_metadata(root)
    assert meta["close"].min is None
    assert meta["close"].max is None


def test_read_dataset_metadata_non_numeric_and_empty(parquet_dir: Path) -> None:
    root = parquet_dir / "binance" / "market_type=spot" / "symbol=Y" / "timeframe=1h"
    target = root / "year=2024" / "month=01"
    target.mkdir(parents=True)
    make_ohlcv(3).with_columns(pl.lit("abc").alias("note")).write_parquet(target / "data.parquet")
    meta = read_dataset_metadata(root)
    assert meta["note"].dtype == "String"
    assert meta["note"].min is None
    assert read_dataset_metadata(parquet_dir / "nothing") == {}
