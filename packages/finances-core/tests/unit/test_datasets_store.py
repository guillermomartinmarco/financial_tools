"""T2.3 de ``ft_datasets``: lectura del almacén Hive por ítem y borrado de particiones."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from datasets_helpers import candles, make_item, ms, write_store

from finances_core.datasets import store
from finances_core.datasets.errors import DatasetDataError

pytestmark = pytest.mark.parametrize("exchange", ["binance", "yfinance"])


def _timeframe(exchange: str) -> str:
    return "1h" if exchange == "binance" else "1d"


def _store_with_neighbours(datasets_dir: Path, exchange: str) -> Any:
    """Ítem Q1 2024 con datos también en dic-2023 y abr-2024 (meses que no solapan)."""
    item = make_item(exchange, timeframe=_timeframe(exchange))
    write_store(datasets_dir, item, candles(item, ms(2023, 12, 1), ms(2024, 5, 1)))
    return item


def test_scan_reads_only_overlapping_months(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exchange: str
) -> None:
    item = _store_with_neighbours(tmp_path, exchange)
    read: list[str] = []
    original = pl.scan_parquet

    def spy(source: Any, *args: Any, **kwargs: Any) -> pl.LazyFrame:
        read.append(Path(source).parent.name + "/" + Path(source).parent.parent.name)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(pl, "scan_parquet", spy)
    months = [month for month, _ in store.scan_item(tmp_path, item)]
    assert months == ["2024-01", "2024-02", "2024-03"]
    assert sorted(read) == ["month=01/year=2024", "month=02/year=2024", "month=03/year=2024"]


def test_scan_filters_range_and_excludes_end_candle(tmp_path: Path, exchange: str) -> None:
    item = make_item(exchange, timeframe=_timeframe(exchange), end=date(2024, 3, 15))
    write_store(tmp_path, item, candles(item, ms(2024, 1, 1), ms(2024, 4, 1)))
    frames = [frame for _, frame in store.scan_item(tmp_path, item)]
    times = pl.concat(frames)["open_time_ms"].to_list()
    assert min(times) == item.start_ms
    assert max(times) < item.end_ms
    assert item.end_ms not in times
    assert store.count_rows(tmp_path, item) == len(times)
    strict, informational = store.item_columns(item)
    assert frames[0].columns == [*strict, *informational]


def test_missing_item_yields_nothing(tmp_path: Path, exchange: str) -> None:
    item = make_item(exchange, timeframe=_timeframe(exchange))
    assert list(store.scan_item(tmp_path, item)) == []
    assert store.count_rows(tmp_path, item) == 0


def test_duplicate_open_time_is_data_error(tmp_path: Path, exchange: str) -> None:
    item = make_item(exchange, timeframe=_timeframe(exchange))
    frame = candles(item, ms(2024, 2, 1), ms(2024, 2, 10))
    path = Path(write_store(tmp_path, item, frame)[0])
    pl.read_parquet(path).pipe(lambda df: pl.concat([df, df.tail(1)])).write_parquet(path)
    with pytest.raises(DatasetDataError, match="open_time_ms"):
        list(store.scan_item(tmp_path, item))


def test_non_canonical_dtype_is_data_error(tmp_path: Path, exchange: str) -> None:
    item = make_item(exchange, timeframe=_timeframe(exchange))
    path = Path(write_store(tmp_path, item, candles(item, ms(2024, 2, 1), ms(2024, 2, 10)))[0])
    pl.read_parquet(path).with_columns(pl.col("close").cast(pl.Float64)).write_parquet(path)
    with pytest.raises(DatasetDataError, match="close"):
        list(store.scan_item(tmp_path, item))


def test_missing_strict_column_is_data_error(tmp_path: Path, exchange: str) -> None:
    item = make_item(exchange, timeframe=_timeframe(exchange))
    path = Path(write_store(tmp_path, item, candles(item, ms(2024, 2, 1), ms(2024, 2, 10)))[0])
    pl.read_parquet(path).drop("volume").write_parquet(path)
    with pytest.raises(DatasetDataError, match="volume"):
        list(store.scan_item(tmp_path, item))


def test_delete_only_overlapping_months(tmp_path: Path, exchange: str) -> None:
    datasets_dir = tmp_path / "datasets"
    item = _store_with_neighbours(datasets_dir, exchange)
    sibling = tmp_path / "parquet"
    write_store(sibling, item, candles(item))  # mismo layout fuera de datasets_dir
    before_sibling = sorted(p for p in sibling.rglob("*") if p.is_file())
    deleted = store.delete_item_partitions(datasets_dir, item)
    assert [p.name for p in deleted] == ["month=01", "month=02", "month=03"]
    remaining = sorted(
        f"{p.parent.parent.name}/{p.parent.name}" for p in datasets_dir.rglob("data.parquet")
    )
    assert remaining == ["year=2023/month=12", "year=2024/month=04"]
    assert sorted(p for p in sibling.rglob("*") if p.is_file()) == before_sibling
    assert store.delete_item_partitions(datasets_dir, item) == []  # idempotente


def test_delete_refuses_paths_outside_datasets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exchange: str
) -> None:
    datasets_dir = tmp_path / "datasets"
    outside = tmp_path / "outside"
    item = make_item(exchange, timeframe=_timeframe(exchange))
    write_store(outside, item, candles(item))
    real_item_dir = store.item_dir
    monkeypatch.setattr(store, "item_dir", lambda _d, i: real_item_dir(outside, i))
    with pytest.raises(DatasetDataError, match="fuera"):
        store.delete_item_partitions(datasets_dir, item)
    assert len(list(outside.rglob("data.parquet"))) == 3
