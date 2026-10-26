"""T5.3 — Todas las recetas versionadas en ``datasets/`` son válidas y reproducibles (sin red).

Si existe su *lock*, debe ser válido (``ftds-v1``) y corresponder a la receta actual
(``recipe_sha256``) con un ítem por cada ítem de la receta, en el mismo orden.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from finances_core.config import FinancesSettings
from finances_core.datasets import recipes
from typer.testing import CliRunner

from finances_downloader.ft_datasets import cli

REPO_DATASETS = Path(__file__).resolve().parents[4] / "datasets"
REPO_RECIPES = recipes.list_recipes(REPO_DATASETS)


def test_repo_ships_benchmark_v1() -> None:
    assert "benchmark-v1" in REPO_RECIPES


@pytest.mark.parametrize("name", REPO_RECIPES)
def test_repo_recipe_is_valid_and_reproducible(name: str) -> None:
    recipe = recipes.load_recipe(REPO_DATASETS, name)
    today = datetime.now(UTC).date()
    recipes.validate_reproducible(recipe, today, FinancesSettings().DATASETS_SETTLE_DAYS)
    lock = recipes.load_lock(REPO_DATASETS, name)
    if lock is not None:
        assert lock.recipe_sha256 == recipe.canonical_sha256()
        assert [i.key for i in lock.items] == [i.key for i in recipe.items()]


def test_show_benchmark_v1_lists_seven_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_DATASETS_RECIPES_DIR", str(REPO_DATASETS))
    result = CliRunner().invoke(cli.app, ["--quiet", "show", "benchmark-v1"])
    assert result.exit_code == 0, result.output
    keys = [item["key"] for item in json.loads(result.stdout)["items"]]
    assert keys == [
        "binance/spot/BTCUSDT/1m@2024-01-01..2024-04-01",
        "binance/spot/BTCUSDT/1h@2024-01-01..2024-04-01",
        "binance/spot/ETHUSDT/1m@2024-01-01..2024-04-01",
        "binance/spot/ETHUSDT/1h@2024-01-01..2024-04-01",
        "yfinance/equity/AAPL/1d@2015-01-01..2025-01-01",
        "yfinance/index/^GSPC/1d@2015-01-01..2025-01-01",
        "yfinance/fx/EURUSD=X/1d@2015-01-01..2025-01-01",
    ]
