"""T2.2/T2.3 — Inventario local (layout Hive RF-4) y cálculo de gaps."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from finances_downloader.ft_market_data_binance.inventory import (
    compute_gaps,
    inventory_local_parquet,
    partition_base_dir,
)
from finances_downloader.ft_market_data_binance.normalizer import normalize_rest_json
from finances_downloader.ft_market_data_binance.writer import write_parquet_partitioned

from .conftest import kline_row, ms

H = 3_600_000


def _write(parquet_dir: Path, symbol: str, market: str, start: int, n: int) -> list[str]:
    rows = [kline_row(start + i * H) for i in range(n)]
    df = normalize_rest_json(rows, symbol, "1h", market, start, start + n * H)
    return write_parquet_partitioned(df, parquet_dir)


def test_partition_base_dir_is_full_hive_layout(tmp_path: Path) -> None:
    assert partition_base_dir(tmp_path, "btcusdt", "1h", "spot") == (
        tmp_path / "binance" / "market_type=spot" / "symbol=BTCUSDT" / "timeframe=1h"
    )


def test_inventory_reads_writer_layout(tmp_path: Path) -> None:
    # 2 meses: 31-ene 22:00 -> 1-feb 02:00
    _write(tmp_path, "BTCUSDT", "spot", ms(2024, 1, 31, 22), 5)
    ranges = inventory_local_parquet(tmp_path, "BTCUSDT", "1h", "spot")
    assert ranges == [
        (ms(2024, 1, 31, 22), ms(2024, 1, 31, 23)),
        (ms(2024, 2, 1, 0), ms(2024, 2, 1, 2)),
    ]


def test_inventory_isolates_market_symbol_and_timeframe(tmp_path: Path) -> None:
    _write(tmp_path, "BTCUSDT", "spot", ms(2024, 1, 1), 3)
    _write(tmp_path, "ETHUSDT", "futures", ms(2024, 3, 1), 3)
    assert inventory_local_parquet(tmp_path, "BTCUSDT", "1h", "futures") == []
    assert inventory_local_parquet(tmp_path, "ETHUSDT", "1h", "spot") == []
    assert inventory_local_parquet(tmp_path, "BTCUSDT", "5m", "spot") == []
    assert inventory_local_parquet(tmp_path, "ETHUSDT", "1h", "futures") == [
        (ms(2024, 3, 1), ms(2024, 3, 1, 2))
    ]


def test_inventory_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert inventory_local_parquet(tmp_path / "nope", "BTCUSDT", "1h", "spot") == []


def test_inventory_ignores_temp_and_corrupt_files(tmp_path: Path) -> None:
    _write(tmp_path, "BTCUSDT", "spot", ms(2024, 1, 1), 2)
    part = partition_base_dir(tmp_path, "BTCUSDT", "1h", "spot") / "year=2024" / "month=01"
    (part / ".data.parquet.tmp").write_bytes(b"garbage")
    bad = partition_base_dir(tmp_path, "BTCUSDT", "1h", "spot") / "year=2024" / "month=05"
    bad.mkdir(parents=True)
    (bad / "data.parquet").write_bytes(b"not parquet")
    assert inventory_local_parquet(tmp_path, "BTCUSDT", "1h", "spot") == [
        (ms(2024, 1, 1), ms(2024, 1, 1, 1))
    ]


def test_inventory_uses_aggregation_without_loading_all_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "BTCUSDT", "spot", ms(2024, 1, 1), 2)
    calls: list[object] = []
    real_scan = pl.scan_parquet

    def spy(*args: object, **kwargs: object) -> pl.LazyFrame:
        calls.append(args[0])
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(pl, "scan_parquet", spy)
    inventory_local_parquet(tmp_path, "BTCUSDT", "1h", "spot")
    assert len(calls) == 1


# -------------------------------------------------------------------- gaps
def test_gaps_without_local_data() -> None:
    assert compute_gaps(0, 100, []) == [(0, 100)]


def test_gaps_fully_covered() -> None:
    assert compute_gaps(10, 90, [(0, 100)]) == []


def test_gaps_partial() -> None:
    assert compute_gaps(0, 100, [(20, 40), (60, 80)]) == [(0, 20), (40, 60), (80, 100)]


def test_gaps_merge_adjacent_and_overlapping() -> None:
    assert compute_gaps(0, 100, [(10, 30), (30, 50), (45, 70)]) == [(0, 10), (70, 100)]


def test_gaps_ignore_ranges_outside_request() -> None:
    assert compute_gaps(50, 100, [(0, 10), (200, 300)]) == [(50, 100)]
