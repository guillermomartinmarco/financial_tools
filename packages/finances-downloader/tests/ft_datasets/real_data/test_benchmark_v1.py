"""T5.4 — ``benchmark-v1`` real: todos los ítems ``OK`` y BTCUSDT ``1m`` sin huecos
inesperados frente a ``missing_candles`` del *lock* (marcador ``real_data``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from finances_core.config import FinancesSettings
from finances_core.datasets import recipes
from finances_core.datasets.testing import require_dataset

RECIPE = "benchmark-v1"
REPO_DATASETS = Path(__file__).resolve().parents[5] / "datasets"
BTC_1M = "binance/spot/BTCUSDT/1m@2024-01-01..2024-04-01"
MINUTE_MS = 60_000
#: Huecos tolerados en Q1 2024 (mantenimientos de Binance): < 0,1 % de las velas.
MAX_MISSING_RATIO = 1e-3

pytestmark = pytest.mark.real_data


def _keys() -> list[str]:
    return [item.key for item in recipes.load_recipe(REPO_DATASETS, RECIPE).items()]


@pytest.mark.parametrize("key", _keys())
def test_every_item_verifies_ok(key: str) -> None:
    frame = require_dataset(RECIPE, key).collect()
    assert frame.height > 0
    assert frame["open_time_ms"].is_sorted()


def test_btcusdt_1m_has_no_unexpected_gaps() -> None:
    frame = require_dataset(RECIPE, BTC_1M).select("open_time_ms").collect()
    settings = FinancesSettings()
    lock = recipes.load_lock(settings.DATASETS_RECIPES_DIR, RECIPE)
    assert lock is not None
    item = next(i for i in lock.items if i.key == BTC_1M)
    assert item.missing_candles is not None
    times = frame["open_time_ms"]
    head = (int(times[0]) - item.start_ms) // MINUTE_MS
    tail = (item.end_ms - MINUTE_MS - int(times[-1])) // MINUTE_MS
    inner = int((times.diff().drop_nulls() // MINUTE_MS - 1).sum())
    assert head + inner + tail == item.missing_candles
    assert times.diff().drop_nulls().min() == MINUTE_MS
    assert item.missing_candles <= MAX_MISSING_RATIO * (item.end_ms - item.start_ms) / MINUTE_MS
