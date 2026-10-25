"""T1.2: inventario local (``exchange``, ``market_type="*"``) y cálculo de gaps."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from finances_core.marketdata.inventory import (
    ANY_MARKET_TYPE,
    compute_gaps,
    coverage_ranges,
    inventory_local_parquet,
    partition_base_dir,
)

DAY_MS = 86_400_000


def _write(base: Path, market_type: str, symbol: str, month: int, ts: list[int]) -> None:
    target = (
        base
        / f"market_type={market_type}"
        / f"symbol={symbol}"
        / "timeframe=1d"
        / "year=2024"
        / f"month={month:02d}"
    )
    target.mkdir(parents=True)
    pl.DataFrame({"open_time_ms": ts, "close": [1.0] * len(ts)}).write_parquet(
        target / "data.parquet"
    )


def test_partition_base_dir_uses_exchange(tmp_path: Path) -> None:
    assert partition_base_dir(tmp_path, "yfinance", "AAPL", "1d", "equity") == (
        tmp_path / "yfinance" / "market_type=equity" / "symbol=AAPL" / "timeframe=1d"
    )


def test_inventory_specific_market_type(tmp_path: Path) -> None:
    _write(tmp_path / "yfinance", "equity", "AAPL", 1, [100, 200])
    _write(tmp_path / "yfinance", "etf", "AAPL", 2, [300, 400])
    ranges = inventory_local_parquet(tmp_path, "AAPL", "1d", "equity", exchange="yfinance")
    assert ranges == [(100, 200)]


def test_inventory_any_market_type_finds_all(tmp_path: Path) -> None:
    _write(tmp_path / "yfinance", "equity", "EURUSD=X", 1, [100, 200])
    _write(tmp_path / "yfinance", "fx", "EURUSD=X", 2, [300, 400])
    _write(tmp_path / "yfinance", "fx", "OTHER", 2, [999])
    ranges = inventory_local_parquet(
        tmp_path, "EURUSD=X", "1d", ANY_MARKET_TYPE, exchange="yfinance"
    )
    assert ranges == [(100, 200), (300, 400)]


def test_inventory_ignores_other_exchange_and_missing_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "binance", "spot", "AAPL", 1, [1, 2])
    assert inventory_local_parquet(tmp_path, "AAPL", "1d", "*", exchange="yfinance") == []
    assert inventory_local_parquet(tmp_path, "AAPL", "1d", "equity", exchange="yfinance") == []


def test_inventory_keeps_symbol_verbatim(tmp_path: Path) -> None:
    _write(tmp_path / "yfinance", "index", "^GSPC", 1, [5, 6])
    assert inventory_local_parquet(tmp_path, "^GSPC", "1d", "*", exchange="yfinance") == [(5, 6)]


def test_inventory_skips_corrupt_partition(tmp_path: Path) -> None:
    _write(tmp_path / "yfinance", "equity", "AAPL", 1, [100, 200])
    bad = partition_base_dir(tmp_path, "yfinance", "AAPL", "1d", "equity") / "year=2024/month=02"
    bad.mkdir(parents=True)
    (bad / "data.parquet").write_bytes(b"not parquet")
    assert inventory_local_parquet(tmp_path, "AAPL", "1d", "*", exchange="yfinance") == [(100, 200)]


def test_gaps_with_coverage_until_last_candle_plus_timeframe() -> None:
    """Una partición cubre desde su primera vela hasta la última + 1 timeframe (RF-6)."""
    covered = coverage_ranges([(0, 9 * DAY_MS)], DAY_MS)
    assert covered == [(0, 10 * DAY_MS)]
    assert compute_gaps(0, 10 * DAY_MS, covered) == []
    assert compute_gaps(0, 15 * DAY_MS, covered) == [(10 * DAY_MS, 15 * DAY_MS)]


def test_compute_gaps_complement_and_merge() -> None:
    assert compute_gaps(0, 100, []) == [(0, 100)]
    assert compute_gaps(0, 100, [(10, 20), (15, 30), (50, 60)]) == [(0, 10), (30, 50), (60, 100)]
    assert compute_gaps(0, 100, [(0, 100)]) == []
