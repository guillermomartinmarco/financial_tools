"""T2.4 de ``ft_datasets``: ``compute_item_lock`` y ``verify_recipe`` (RF-5, RF-6)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from datasets_helpers import PRICE_COLUMNS, candles, ms, write_store

from finances_core.datasets.verify import (
    build_recipe_lock,
    compute_item_lock,
    expected_candles,
    verify_item,
    verify_recipe,
)
from finances_core.schemas.datasets import DatasetRecipe, RecipeItem, RecipeLock

LOCKED_AT = datetime(2026, 10, 1, tzinfo=UTC)


def _recipe(end: date = date(2024, 4, 1)) -> DatasetRecipe:
    return DatasetRecipe.model_validate(
        {
            "schema_version": 1,
            "name": "demo-v1",
            "datasets": [
                {
                    "exchange": "binance",
                    "market_type": "spot",
                    "symbols": ["BTCUSDT"],
                    "timeframes": ["1h"],
                    "start": date(2024, 1, 1),
                    "end": end,
                },
                {
                    "exchange": "yfinance",
                    "market_type": "equity",
                    "symbols": ["AAPL"],
                    "timeframes": ["1d"],
                    "start": date(2024, 1, 1),
                    "end": date(2024, 4, 1),
                },
            ],
        }
    )


def _items() -> tuple[RecipeItem, RecipeItem]:
    binance, yahoo = _recipe().items()
    return binance, yahoo


def _materialize(datasets_dir: Path) -> None:
    for item in _items():
        write_store(datasets_dir, item, candles(item))


def _lock(datasets_dir: Path, recipe: DatasetRecipe | None = None) -> RecipeLock:
    recipe = recipe or _recipe()
    locks = [compute_item_lock(datasets_dir, item) for item in recipe.items()]
    assert all(lock is not None for lock in locks)
    return build_recipe_lock(
        recipe, [lock for lock in locks if lock], locked_at=LOCKED_AT, tool_versions={}
    )


def _rewrite(datasets_dir: Path, item: RecipeItem, frame: pl.DataFrame) -> None:
    for path in datasets_dir.rglob("data.parquet"):
        if f"symbol={item.symbol}" in str(path):
            path.unlink()
    write_store(datasets_dir, item, frame)


# --- compute_item_lock ---------------------------------------------------------------------


def test_compute_item_lock_fields(tmp_path: Path) -> None:
    _materialize(tmp_path)
    binance, yahoo = _items()
    lock = compute_item_lock(tmp_path, binance)
    assert lock is not None
    assert lock.key == binance.key
    assert lock.row_count == 2184
    assert (lock.first_open_time_ms, lock.last_open_time_ms) == (
        binance.start_ms,
        binance.end_ms - 3_600_000,
    )
    assert sorted(lock.month_hashes) == ["2024-01", "2024-02", "2024-03"]
    assert [p.row for p in lock.probes] == [0, 1092, 2183]
    assert lock.probes[0].close_bits == "0x42c88000"  # 100.25 en float32
    assert lock.probes[0].close == 100.25
    assert lock.informational == {}
    assert lock.missing_candles == 0
    yahoo_lock = compute_item_lock(tmp_path, yahoo)
    assert yahoo_lock is not None
    assert set(yahoo_lock.informational) == {"adj_close"}
    assert yahoo_lock.missing_candles is None  # solo binance


def test_missing_candles_counts_removed_rows(tmp_path: Path) -> None:
    binance, _ = _items()
    removed = {ms(2024, 2, 10, 5), ms(2024, 2, 10, 6), ms(2024, 3, 1, 0)}
    write_store(
        tmp_path, binance, candles(binance).filter(~pl.col("open_time_ms").is_in(list(removed)))
    )
    lock = compute_item_lock(tmp_path, binance)
    assert lock is not None
    assert lock.missing_candles == 3


def test_compute_item_lock_none_without_rows(tmp_path: Path) -> None:
    binance, _ = _items()
    assert compute_item_lock(tmp_path, binance) is None


@pytest.mark.parametrize(
    ("timeframe", "start", "end", "expected"),
    [
        ("1h", date(2024, 1, 1), date(2024, 4, 1), 2184),
        ("1m", date(2024, 1, 1), date(2024, 4, 1), 131_040),
        ("1d", date(2024, 1, 1), date(2024, 4, 1), 91),
        ("1w", date(2024, 1, 1), date(2024, 4, 1), 13),  # lunes 2024-01-01 .. 2024-03-25
        ("1M", date(2024, 1, 1), date(2024, 4, 1), 3),
        ("1M", date(2024, 1, 15), date(2024, 4, 1), 2),
    ],
)
def test_expected_candles(timeframe: str, start: date, end: date, expected: int) -> None:
    item = RecipeItem(
        exchange="binance",
        market_type="spot",
        symbol="X",
        timeframe=timeframe,
        start_ms=int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000),
        end_ms=int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp() * 1000),
    )
    assert expected_candles(item) == expected


# --- verify_recipe: estados RF-5 -----------------------------------------------------------


def test_ok(tmp_path: Path) -> None:
    _materialize(tmp_path)
    report = verify_recipe(_recipe(), _lock(tmp_path), tmp_path)
    assert report.status == "OK"
    assert not report.recipe_changed
    assert [i.status for i in report.items] == ["OK", "OK"]
    assert all(i.content_hash == i.expected_content_hash for i in report.items)
    assert report.duration_ms >= 0


def test_adj_close_change_is_warning(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    _, yahoo = _items()
    _rewrite(tmp_path, yahoo, candles(yahoo).with_columns(pl.col("adj_close") * 0.5))
    report = verify_recipe(_recipe(), lock, tmp_path)
    assert report.status == "OK"
    item = report.items[1]
    assert item.status == "OK_WITH_WARNINGS"
    assert any("adj_close" in w for w in item.warnings)


def test_missing(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    _, yahoo = _items()
    for path in tmp_path.rglob("data.parquet"):
        if "symbol=AAPL" in str(path):
            path.unlink()
    report = verify_recipe(_recipe(), lock, tmp_path)
    assert report.status == "MISMATCH"
    item = report.items[1]
    assert (item.status, item.row_count, item.content_hash) == ("MISSING", 0, None)
    assert item.expected_row_count == lock.items[1].row_count
    assert item.action


def test_unlocked_without_lock_or_item(tmp_path: Path) -> None:
    _materialize(tmp_path)
    report = verify_recipe(_recipe(), None, tmp_path)
    assert report.status == "UNLOCKED"
    assert [i.status for i in report.items] == ["UNLOCKED", "UNLOCKED"]
    assert report.items[0].content_hash is not None
    partial = _lock(tmp_path).model_copy(update={"items": [_lock(tmp_path).items[0]]})
    report = verify_recipe(_recipe(), partial, tmp_path)
    assert [i.status for i in report.items] == ["OK", "UNLOCKED"]
    assert report.status == "UNLOCKED"


def test_missing_without_lock_is_missing(tmp_path: Path) -> None:
    report = verify_recipe(_recipe(), None, tmp_path)
    assert [i.status for i in report.items] == ["MISSING", "MISSING"]
    assert report.status == "MISMATCH"


def test_row_removed_is_rows_changed(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    binance, _ = _items()
    _rewrite(tmp_path, binance, candles(binance).filter(pl.col("open_time_ms") != ms(2024, 2, 3)))
    item = verify_recipe(_recipe(), lock, tmp_path).items[0]
    assert (item.status, item.drift) == ("MISMATCH", "ROWS_CHANGED")
    assert item.row_count == 2183
    assert item.months_changed == ["2024-02"]
    assert "open_time_ms" in item.columns_changed
    assert item.action is not None
    assert "--refetch" in item.action
    assert "--update" in item.action


def test_prices_scaled_is_scale_restated(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    _, yahoo = _items()
    scaled = candles(yahoo).with_columns([pl.col(c) * 0.25 for c in PRICE_COLUMNS])
    _rewrite(tmp_path, yahoo, scaled)
    report = verify_recipe(_recipe(), lock, tmp_path)
    item = report.items[1]
    assert (item.status, item.drift) == ("MISMATCH", "SCALE_RESTATED")
    assert item.scale_factor == pytest.approx(0.25)
    assert item.columns_changed == list(PRICE_COLUMNS)
    assert item.months_changed == ["2024-01", "2024-02", "2024-03"]
    assert report.status == "MISMATCH"


def test_partial_price_change_is_not_scale(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    _, yahoo = _items()
    _rewrite(tmp_path, yahoo, candles(yahoo).with_columns(pl.col("close") * 0.25))
    item = verify_recipe(_recipe(), lock, tmp_path).items[1]
    assert (item.drift, item.columns_changed) == ("VALUES_CHANGED", ["close"])


def test_single_close_altered_in_march_is_values_changed(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    binance, _ = _items()
    target = ms(2024, 3, 12, 7)
    altered = candles(binance).with_columns(
        pl.when(pl.col("open_time_ms") == target)
        .then(pl.col("close") + 1.0)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    _rewrite(tmp_path, binance, altered)
    item = verify_recipe(_recipe(), lock, tmp_path).items[0]
    assert (item.status, item.drift, item.scale_factor) == ("MISMATCH", "VALUES_CHANGED", None)
    assert item.columns_changed == ["close"]
    assert item.months_changed == ["2024-03"]


def test_recipe_edited_is_recipe_changed(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    edited = _recipe(end=date(2024, 3, 1))
    report = verify_recipe(edited, lock, tmp_path)
    assert report.recipe_changed
    assert report.status == "RECIPE_CHANGED"
    assert [i.status for i in report.items] == ["UNLOCKED", "OK"]  # todos se evalúan


def test_verify_item_matches_recipe_report(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    binance, _ = _items()
    assert (
        verify_item(tmp_path, binance, lock.items[0])
        == (verify_recipe(_recipe(), lock, tmp_path).items[0])
    )


def test_build_recipe_lock(tmp_path: Path) -> None:
    _materialize(tmp_path)
    lock = _lock(tmp_path)
    assert lock.recipe == "demo-v1"
    assert lock.recipe_sha256 == _recipe().canonical_sha256()
    assert [i.key for i in lock.items] == [i.key for i in _recipe().items()]
    assert lock.hash_algorithm == "ftds-v1"
