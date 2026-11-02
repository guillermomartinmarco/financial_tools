"""T1.3: ``plan`` + snapshot + ``commit`` optimista sobre un almacén Parquet real."""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl
import pytest
from finances_core.schemas import DatasetSelector

from finances_downloader.ft_market_data_manager import planner

from .conftest import (
    FEB_2024_MS,
    HOUR_MS,
    JAN_2024_MS,
    MAR_2024_MS,
    hours,
    make_ohlcv,
    write_partition_files,
)


def _build(parquet_dir: Path) -> None:
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    write_partition_files(parquet_dir, 2024, 2, [make_ohlcv(hours(FEB_2024_MS, 3))])
    write_partition_files(parquet_dir, 2024, 3, [make_ohlcv(hours(MAR_2024_MS, 3))])
    write_partition_files(
        parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))], symbol="ETHUSDT"
    )
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [make_ohlcv(hours(JAN_2024_MS, 3))],
        exchange="yfinance",
        market_type="equity",
        symbol="AAPL",
        timeframe="1d",
    )


def _months(parts: list[planner.Partition]) -> list[tuple[int, int]]:
    return [(p.year, p.month) for p in parts]


def test_plan_selects_everything_without_filters(parquet_dir: Path) -> None:
    _build(parquet_dir)
    parts = planner.plan(DatasetSelector(), parquet_dir)
    assert len(parts) == 5
    assert all(p.files and p.snapshot for p in parts)


def test_plan_filters_by_each_dataset_field_with_and(parquet_dir: Path) -> None:
    _build(parquet_dir)

    def count(**kw: list[str]) -> int:
        return len(planner.plan(DatasetSelector(**kw), parquet_dir))

    assert count(exchange=["binance"]) == 4
    assert count(market_type=["equity"]) == 1
    assert count(symbol=["ETHUSDT"]) == 1
    assert count(timeframe=["1d"]) == 1
    assert count(symbol=["BTCUSDT", "ETHUSDT"]) == 4
    assert count(exchange=["yfinance"], symbol=["BTCUSDT"]) == 0  # AND entre campos


def test_plan_filters_by_date_range(parquet_dir: Path) -> None:
    _build(parquet_dir)
    btc = ["BTCUSDT"]
    feb = DatasetSelector(symbol=btc, start_ms=FEB_2024_MS, end_ms=MAR_2024_MS - 1)
    assert _months(planner.plan(feb, parquet_dir)) == [(2024, 2)]
    from_feb = DatasetSelector(symbol=btc, start_ms=FEB_2024_MS)
    assert _months(planner.plan(from_feb, parquet_dir)) == [(2024, 2), (2024, 3)]
    until_jan = DatasetSelector(symbol=btc, end_ms=FEB_2024_MS - 1)
    assert _months(planner.plan(until_jan, parquet_dir)) == [(2024, 1)]
    mid = DatasetSelector(symbol=btc, start_ms=JAN_2024_MS + 5, end_ms=JAN_2024_MS + 10)
    assert _months(planner.plan(mid, parquet_dir)) == [(2024, 1)]


def test_plan_groups_files_of_a_partition_sorted(parquet_dir: Path) -> None:
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [make_ohlcv(hours(JAN_2024_MS, 2)), make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2))],
        names=["b.parquet", "a.parquet"],
    )
    (part,) = planner.plan(DatasetSelector(), parquet_dir)
    assert [f.name for f in part.files] == ["a.parquet", "b.parquet"]
    assert [s[0] for s in part.snapshot] == ["a.parquet", "b.parquet"]
    assert part.label == "binance/spot/BTCUSDT/1h"


def test_plan_ignores_missing_dir_and_non_candle_trees(parquet_dir: Path) -> None:
    assert planner.plan(DatasetSelector(), parquet_dir / "nope") == []
    events = parquet_dir / "yfinance_events" / "symbol=AAPL"
    events.mkdir(parents=True)
    pl.DataFrame({"a": [1]}).write_parquet(events / "events.parquet")
    assert planner.plan(DatasetSelector(), parquet_dir) == []


def test_commit_write_replaces_atomically_and_removes_leftovers_only_after(
    parquet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = write_partition_files(
        parquet_dir,
        2024,
        1,
        [make_ohlcv(hours(JAN_2024_MS, 2)), make_ohlcv(hours(JAN_2024_MS + 5 * HOUR_MS, 2))],
    )
    (part,) = planner.plan(DatasetSelector(), parquet_dir)
    out = pl.concat([pl.read_parquet(f) for f in files])

    seen: list[list[str]] = []
    real_write = planner.atomic_write

    def spy(df: pl.DataFrame, target: Path) -> None:
        real_write(df, target)
        # justo tras el reemplazo atómico aún existen los archivos originales
        seen.append(sorted(p.name for p in target.parent.iterdir()))

    monkeypatch.setattr(planner, "atomic_write", spy)
    assert planner.commit(part, out, parquet_dir) == "changed"

    assert seen == [["data.parquet", "part-000.parquet", "part-001.parquet"]]
    assert sorted(p.name for p in part.directory.iterdir()) == ["data.parquet"]
    assert pl.read_parquet(part.directory / "data.parquet").height == 4


def test_commit_write_failure_keeps_originals(
    parquet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = write_partition_files(
        parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 2)), make_ohlcv([JAN_2024_MS + 9])]
    )
    (part,) = planner.plan(DatasetSelector(), parquet_dir)

    def boom(df: pl.DataFrame, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(planner, "atomic_write", boom)
    with pytest.raises(OSError, match="disk full"):
        planner.commit(part, make_ohlcv(hours(JAN_2024_MS, 1)), parquet_dir)
    assert all(f.exists() for f in files)


def test_commit_write_conflict_when_file_modified_between_plan_and_commit(
    parquet_dir: Path,
) -> None:
    (path,) = write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    (part,) = planner.plan(DatasetSelector(), parquet_dir)
    make_ohlcv(hours(JAN_2024_MS, 5)).write_parquet(path)  # un descargador reescribe el archivo
    before = path.read_bytes()
    assert planner.commit(part, make_ohlcv(hours(JAN_2024_MS, 1)), parquet_dir) == "conflict"
    assert path.read_bytes() == before
    assert sorted(p.name for p in part.directory.iterdir()) == [path.name]


def test_commit_conflict_on_mtime_only_change(parquet_dir: Path) -> None:
    (path,) = write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    (part,) = planner.plan(DatasetSelector(), parquet_dir)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert planner.commit(part, None, parquet_dir) == "conflict"
    assert path.exists()


def test_commit_conflict_when_new_file_appears(parquet_dir: Path) -> None:
    (path,) = write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    (part,) = planner.plan(DatasetSelector(), parquet_dir)
    make_ohlcv([JAN_2024_MS + 99 * HOUR_MS]).write_parquet(path.with_name("z.parquet"))
    assert planner.commit(part, make_ohlcv(hours(JAN_2024_MS, 1)), parquet_dir) == "conflict"
    assert sorted(p.name for p in part.directory.iterdir()) == [path.name, "z.parquet"]


def test_commit_delete_removes_files_and_empty_hive_dirs(parquet_dir: Path) -> None:
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    write_partition_files(parquet_dir, 2024, 2, [make_ohlcv(hours(FEB_2024_MS, 3))])
    write_partition_files(
        parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))], symbol="ETHUSDT"
    )
    jan = planner.plan(DatasetSelector(symbol=["BTCUSDT"], end_ms=FEB_2024_MS - 1), parquet_dir)
    assert len(jan) == 1
    assert planner.commit(jan[0], None, parquet_dir) == "changed"
    assert not jan[0].directory.exists()  # month=01 vacío
    assert (jan[0].directory.parent / "month=02").exists()  # year=2024 aún tiene febrero
    feb = planner.plan(DatasetSelector(symbol=["BTCUSDT"]), parquet_dir)
    assert planner.commit(feb[0], None, parquet_dir) == "changed"
    assert not (parquet_dir / "binance" / "market_type=spot" / "symbol=BTCUSDT").exists()
    assert (parquet_dir / "binance" / "market_type=spot" / "symbol=ETHUSDT").exists()
    assert parquet_dir.exists()


def test_commit_delete_keeps_dirs_with_foreign_content(parquet_dir: Path) -> None:
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    (part,) = planner.plan(DatasetSelector(), parquet_dir)
    (part.directory / "notes.txt").write_text("keep")
    assert planner.commit(part, None, parquet_dir) == "changed"
    assert (part.directory / "notes.txt").exists()
    assert not list(part.directory.glob("*.parquet"))
