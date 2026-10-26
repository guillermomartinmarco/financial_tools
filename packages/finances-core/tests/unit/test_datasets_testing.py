"""T2.5 de ``ft_datasets``: ``require_dataset`` (RF-10) nunca omite en silencio."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from datasets_helpers import candles, ms, write_store

from finances_core.config import FinancesSettings
from finances_core.datasets.recipes import load_recipe, write_lock
from finances_core.datasets.testing import require_dataset
from finances_core.datasets.verify import build_recipe_lock, compute_item_lock

RECIPE = """\
schema_version = 1
name = "demo-v1"

[[datasets]]
exchange = "binance"
market_type = "spot"
symbols = ["BTCUSDT", "ETHUSDT"]
timeframes = ["1h"]
start = 2024-01-01
end = 2024-04-01
"""
BTC = "binance/spot/BTCUSDT/1h@2024-01-01..2024-04-01"
ETH = "binance/spot/ETHUSDT/1h@2024-01-01..2024-04-01"


@pytest.fixture
def settings() -> FinancesSettings:
    """Receta, almacén (con velas fuera de rango) y *lock* bajo ``FT_PROJECT_ROOT`` temporal."""
    cfg = FinancesSettings()
    cfg.DATASETS_RECIPES_DIR.mkdir(parents=True)
    (cfg.DATASETS_RECIPES_DIR / "demo-v1.toml").write_text(RECIPE, encoding="utf-8")
    recipe = load_recipe(cfg.DATASETS_RECIPES_DIR, "demo-v1")
    for item in recipe.items():
        write_store(cfg.DATASETS_DIR, item, candles(item, ms(2023, 12, 20), ms(2024, 4, 5)))
    locks = [compute_item_lock(cfg.DATASETS_DIR, item) for item in recipe.items()]
    write_lock(
        cfg.DATASETS_RECIPES_DIR,
        build_recipe_lock(
            recipe,
            [lock for lock in locks if lock],
            locked_at=datetime(2026, 10, 1, tzinfo=UTC),
            tool_versions={},
        ),
    )
    return cfg


def test_ok_item_returns_lazyframe_of_range(settings: FinancesSettings) -> None:
    lf = require_dataset("demo-v1", BTC)
    assert isinstance(lf, pl.LazyFrame)
    times = lf.select("open_time_ms").collect()["open_time_ms"].to_list()
    assert len(times) == 2184
    assert min(times) == ms(2024, 1, 1)
    assert max(times) == ms(2024, 3, 31, 23)
    assert "close" in lf.collect_schema().names()
    assert require_dataset("demo-v1", BTC, settings=settings).collect().height == 2184


def test_mismatch_fails_with_detail(settings: FinancesSettings) -> None:
    for path in settings.DATASETS_DIR.rglob("data.parquet"):
        if "symbol=ETHUSDT" in str(path) and "month=02" in str(path):
            frame = pl.read_parquet(path)
            frame.with_columns(pl.col("close") * 2).write_parquet(path)
    with pytest.raises(pytest.fail.Exception) as exc_info:
        require_dataset("demo-v1", ETH)
    message = str(exc_info.value)
    assert ETH in message
    assert "MISMATCH" in message
    assert "2024-02" in message
    assert require_dataset("demo-v1", BTC).collect().height == 2184  # el otro ítem sigue OK


def test_missing_fails_with_detail(settings: FinancesSettings) -> None:
    for path in settings.DATASETS_DIR.rglob("data.parquet"):
        if "symbol=ETHUSDT" in str(path):
            path.unlink()
    with pytest.raises(pytest.fail.Exception, match="MISSING"):
        require_dataset("demo-v1", ETH)


def test_unlocked_fails(settings: FinancesSettings) -> None:
    (settings.DATASETS_RECIPES_DIR / "demo-v1.lock.json").unlink()
    with pytest.raises(pytest.fail.Exception, match="UNLOCKED"):
        require_dataset("demo-v1", BTC)


@pytest.mark.parametrize(
    ("recipe", "key", "fragment"),
    [("absent-v1", BTC, "absent-v1"), ("demo-v1", "binance/spot/X/1h@2024-01-01..2024-04-01", "X")],
)
def test_unknown_recipe_or_key_fails(
    settings: FinancesSettings, recipe: str, key: str, fragment: str
) -> None:
    with pytest.raises(pytest.fail.Exception, match=fragment):
        require_dataset(recipe, key)


def test_without_store_fails_clearly(tmp_path: Path) -> None:
    cfg = FinancesSettings()
    cfg.DATASETS_RECIPES_DIR.mkdir(parents=True)
    (cfg.DATASETS_RECIPES_DIR / "demo-v1.toml").write_text(RECIPE, encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="MISSING"):
        require_dataset("demo-v1", BTC)


def test_importing_core_datasets_does_not_import_pytest() -> None:
    code = (
        "import sys\n"
        "import finances_core.datasets\n"
        "import finances_core.datasets.recipes, finances_core.datasets.verify\n"
        "import finances_core.datasets.testing\n"
        "assert 'pytest' not in sys.modules, 'pytest importado'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
