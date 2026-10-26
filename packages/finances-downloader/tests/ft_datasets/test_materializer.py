"""T3.2 — Plan de materialización: ítems omitidos, bloques reducidos, ``--refetch`` y
persistencia con prefijo ``datasets_plan``."""

from __future__ import annotations

from pathlib import Path

from finances_core.config import FinancesSettings
from finances_core.datasets import recipes
from finances_core.marketdata.plans import load_plan, plan_file_path
from finances_core.schemas.datasets import DatasetRecipe, MaterializePlan

from finances_downloader.ft_datasets import materializer, service

from .conftest import AAPL_KEY, ALL_KEYS, BTC_KEY, ETH_KEY, RECIPE_NAME, write_item

TASK = "0192f000-0000-7000-8000-000000000abc"


def _recipe(settings: FinancesSettings) -> DatasetRecipe:
    return recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)


def _fill_and_lock(settings: FinancesSettings) -> DatasetRecipe:
    recipe = _recipe(settings)
    for item in recipe.items():
        write_item(settings.DATASETS_DIR, item)
    assert service.lock_recipe(RECIPE_NAME, settings=settings).outcome == "WRITTEN"
    return recipe


def _plan(settings: FinancesSettings, *, refetch: bool = False) -> MaterializePlan:
    recipe = _recipe(settings)
    lock = recipes.load_lock(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    checks = materializer.precheck(recipe, lock, settings.DATASETS_DIR)
    return materializer.build_plan(recipe, checks, task_id=TASK, created_at_ms=1, refetch=refetch)


def test_all_ok_gives_plan_without_blocks(settings: FinancesSettings, recipe_dir: Path) -> None:
    _fill_and_lock(settings)
    plan = _plan(settings)
    assert plan.skipped_items == ALL_KEYS
    assert plan.blocks == []
    assert plan.refetch_items == []


def test_empty_store_plans_one_block_per_recipe_block(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    plan = _plan(settings)
    assert plan.skipped_items == []
    assert [(b.exchange, b.symbols, b.timeframes) for b in plan.blocks] == [
        ("binance", ["BTCUSDT", "ETHUSDT"], ["1h"]),
        ("yfinance", ["AAPL"], ["1d"]),
    ]


def test_one_pending_symbol_gives_block_with_only_that_symbol(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    recipe = _fill_and_lock(settings)
    eth = next(i for i in recipe.items() if i.key == ETH_KEY)
    assert materializer.store.delete_item_partitions(settings.DATASETS_DIR, eth)
    plan = _plan(settings)
    assert plan.skipped_items == [BTC_KEY, AAPL_KEY]
    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    assert block.symbols == ["ETHUSDT"]
    assert block.timeframes == ["1h"]
    assert block.start_ms == eth.start_ms
    assert block.end_ms_inclusive == eth.end_ms - 1
    assert block.argv[0] == "ft_market_data_binance"
    assert block.argv[-6:] == [
        "--start",
        str(eth.start_ms),
        "--end",
        str(eth.end_ms - 1),
        "--market",
        "spot",
    ]


def test_mismatch_with_refetch_deletes_partitions_and_plans_block(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    recipe = _fill_and_lock(settings)
    aapl = next(i for i in recipe.items() if i.key == AAPL_KEY)
    write_item(settings.DATASETS_DIR, aapl, scale=0.25)

    without = _plan(settings)
    assert without.refetch_items == []
    assert [b.symbols for b in without.blocks] == [["AAPL"]]

    plan = _plan(settings, refetch=True)
    assert plan.refetch_items == [AAPL_KEY]
    assert [b.symbols for b in plan.blocks] == [["AAPL"]]
    month_dir = materializer.store.item_files(settings.DATASETS_DIR, aapl)[0][1].parent
    deleted = materializer.apply_refetch(recipe, plan, settings.DATASETS_DIR)
    assert deleted == [month_dir]
    assert not month_dir.exists()
    btc = next(i for i in recipe.items() if i.key == BTC_KEY)
    assert materializer.store.item_files(settings.DATASETS_DIR, btc)


def test_non_canonical_data_is_mismatch_in_precheck(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    recipe = _fill_and_lock(settings)
    aapl = next(i for i in recipe.items() if i.key == AAPL_KEY)
    path = materializer.store.item_files(settings.DATASETS_DIR, aapl)[0][1]
    path.write_bytes(b"not parquet")
    lock = recipes.load_lock(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    checks = materializer.precheck(recipe, lock, settings.DATASETS_DIR)
    assert checks[AAPL_KEY].status == "MISMATCH"
    assert checks[AAPL_KEY].warnings


def test_plan_persisted_with_datasets_plan_prefix(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    plan = _plan(settings)
    path = materializer.save_materialize_plan(plan, settings)
    assert path == plan_file_path(TASK, settings, prefix="datasets_plan")
    assert path.name == f"datasets_plan_{TASK}.json"
    assert load_plan(TASK, MaterializePlan, settings, prefix="datasets_plan") == plan
    assert materializer.load_materialize_plan(TASK, settings) == plan
    assert materializer.load_materialize_plan("0192f000-0000-7000-8000-000000000000") is None
