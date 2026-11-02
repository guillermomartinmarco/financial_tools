"""Layout Hive compartido por ``ft_data_viewer`` y ``ft_market_data_manager`` (T1.1)."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from finances_core.marketdata import dataset_layout as layout
from finances_core.schemas.data_viewer import DatasetIdentifier

IDENT = DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")


def _write(
    root: Path, ident: DatasetIdentifier, year: int, month: int, name: str = "data.parquet"
) -> Path:
    folder = (
        root
        / ident.exchange
        / f"market_type={ident.market_type}"
        / f"symbol={ident.symbol}"
        / f"timeframe={ident.timeframe}"
        / f"year={year}"
        / f"month={month:02d}"
    )
    folder.mkdir(parents=True)
    path = folder / name
    pl.DataFrame({"open_time_ms": [1]}).write_parquet(path)
    return path


def test_discover_finds_hive_datasets_and_skips_the_rest(tmp_path: Path) -> None:
    _write(tmp_path, IDENT, 2024, 1)
    _write(tmp_path, IDENT.model_copy(update={"market_type": "futures"}), 2024, 1)
    (tmp_path / "binance" / "garbage").mkdir()
    (tmp_path / "yfinance_events" / "symbol=AAPL").mkdir(parents=True)
    found = layout.discover_datasets(tmp_path)
    assert [(i.market_type, i.symbol, i.timeframe) for i, _ in found] == [
        ("futures", "BTCUSDT", "1h"),
        ("spot", "BTCUSDT", "1h"),
    ]


def test_discover_ignores_unknown_market_type(tmp_path: Path) -> None:
    bad = IDENT.model_construct(
        exchange="binance", market_type="options", symbol="X", timeframe="1h"
    )
    _write(tmp_path, bad, 2024, 1)
    assert layout.discover_datasets(tmp_path) == []
    assert layout.discover_datasets(tmp_path / "missing") == []


def test_dataset_files_and_partition_keys(tmp_path: Path) -> None:
    second = _write(tmp_path, IDENT, 2024, 2)
    first = _write(tmp_path, IDENT, 2024, 1)
    root = layout.find_dataset_path(tmp_path, IDENT)
    assert root is not None
    assert layout.dataset_files(root) == [first, second]
    assert [layout.partition_key(f) for f in (first, second)] == [(2024, 1), (2024, 2)]


def test_find_dataset_path_returns_none_when_missing_or_escaping(tmp_path: Path) -> None:
    assert layout.find_dataset_path(tmp_path, IDENT) is None
    (tmp_path / "binance" / "market_type=spot" / "symbol=BTCUSDT" / "timeframe=1h").mkdir(
        parents=True
    )
    assert layout.find_dataset_path(tmp_path, IDENT) is None  # sin archivos Parquet
    evil = IDENT.model_construct(exchange="..", market_type="spot", symbol="x", timeframe="1h")
    assert layout.find_dataset_path(tmp_path, evil) is None


def test_dataset_label() -> None:
    assert layout.dataset_label(IDENT) == "binance/spot/BTCUSDT/1h"


@pytest.mark.parametrize("name", ["year=2024", "market_type="])
def test_hive_value_rejects_non_matching(name: str) -> None:
    assert layout.hive_value(name, "month") is None
