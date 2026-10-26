"""T1.4 de ``ft_datasets``: carga de recetas, rango asentado y *lock* atómico y estable."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from finances_core.datasets.errors import (
    InvalidLockError,
    InvalidRecipeError,
    ItemNotFoundError,
    RecipeNotFoundError,
)
from finances_core.datasets.recipes import (
    canonical_sha256,
    find_item,
    list_recipes,
    load_lock,
    load_recipe,
    lock_path,
    validate_reproducible,
    write_lock,
)
from finances_core.schemas.datasets import HASH_ALGORITHM, ItemLock, Probe, RecipeLock

RECIPE = """\
schema_version = 1
name = "demo-v1"
description = "demo"

[[datasets]]
exchange = "binance"
market_type = "spot"
symbols = ["BTCUSDT", "ETHUSDT"]
timeframes = ["1h"]
start = 2024-01-01
end = 2024-04-01

[[datasets]]
exchange = "yfinance"
market_type = "equity"
symbols = ["AAPL"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01
"""

REORDERED_WITH_COMMENTS = """\
# Comentario de cabecera
schema_version = 1
name = "demo-v1"
description = "otra descripción"   # no participa en el hash

[[datasets]]
exchange = "yfinance"
market_type = "equity"
symbols = ["AAPL"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01

[[datasets]]
exchange = "binance"
market_type = "spot"
symbols = ["ETHUSDT", "BTCUSDT"]   # orden distinto
timeframes = ["1h"]
start = 2024-01-01
end = 2024-04-01
"""


def _write(recipes_dir: Path, name: str, text: str) -> Path:
    recipes_dir.mkdir(parents=True, exist_ok=True)
    path = recipes_dir / f"{name}.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _lock(recipes_dir: Path) -> RecipeLock:
    recipe = load_recipe(recipes_dir, "demo-v1")
    item = recipe.items()[0]
    digest = "sha256:" + "0" * 64
    return RecipeLock(
        lock_version=1,
        hash_algorithm="ftds-v1",
        recipe="demo-v1",
        recipe_sha256=canonical_sha256(recipe),
        locked_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        tool_versions={"polars": "1.6.0", "finances-downloader": "0.1.0"},
        items=[
            ItemLock(
                key=item.key,
                exchange=item.exchange,
                market_type=item.market_type,
                symbol=item.symbol,
                timeframe=item.timeframe,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                row_count=2184,
                first_open_time_ms=item.start_ms,
                last_open_time_ms=item.end_ms - 3_600_000,
                strict_columns=["open_time_ms", "close"],
                content_hash=digest,
                column_hashes={"open_time_ms": digest, "close": digest},
                month_hashes={"2024-01": digest},
                informational={},
                probes=[
                    Probe(row=0, open_time_ms=item.start_ms, close_bits="0x3f800000", close=1.0)
                ],
                missing_candles=0,
            )
        ],
    )


# --- load_recipe / list_recipes ------------------------------------------------------------


def test_load_recipe_and_list(tmp_path: Path) -> None:
    _write(tmp_path, "demo-v1", RECIPE)
    _write(tmp_path, "other.v2", RECIPE.replace('"demo-v1"', '"other.v2"'))
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "Bad Name.toml").write_text("x", encoding="utf-8")
    recipe = load_recipe(tmp_path, "demo-v1")
    assert recipe.name == "demo-v1"
    assert len(recipe.items()) == 3
    assert list_recipes(tmp_path) == ["demo-v1", "other.v2"]


def test_list_recipes_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_recipes(tmp_path / "absent") == []


def test_name_mismatch_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "renamed-v1", RECIPE)
    with pytest.raises(InvalidRecipeError, match="demo-v1"):
        load_recipe(tmp_path, "renamed-v1")


def test_missing_recipe_not_found(tmp_path: Path) -> None:
    with pytest.raises(RecipeNotFoundError, match="nope"):
        load_recipe(tmp_path, "nope")


@pytest.mark.parametrize("name", ["../x", "..", "a/b", "C:\\x", "/etc/passwd", "UPPER", ""])
def test_path_like_names_rejected(tmp_path: Path, name: str) -> None:
    recipes_dir = tmp_path / "recipes"
    _write(tmp_path, "x", RECIPE.replace('"demo-v1"', '"x"'))  # existe fuera de recipes_dir
    with pytest.raises(InvalidRecipeError, match="nombre"):
        load_recipe(recipes_dir, name)
    with pytest.raises(InvalidRecipeError):
        lock_path(recipes_dir, name)


def test_absolute_path_name_rejected(tmp_path: Path) -> None:
    target = _write(tmp_path, "demo-v1", RECIPE)
    with pytest.raises(InvalidRecipeError):
        load_recipe(tmp_path / "recipes", str(target.with_suffix("")))


def test_invalid_toml_and_invalid_model_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "broken", "name = [")
    with pytest.raises(InvalidRecipeError, match="TOML"):
        load_recipe(tmp_path, "broken")
    _write(tmp_path, "demo-v1", RECIPE.replace('"1h"', '"7h"'))
    with pytest.raises(InvalidRecipeError, match="7h") as exc_info:
        load_recipe(tmp_path, "demo-v1")
    assert exc_info.value.detail


def test_find_item(tmp_path: Path) -> None:
    _write(tmp_path, "demo-v1", RECIPE)
    recipe = load_recipe(tmp_path, "demo-v1")
    key = "yfinance/equity/AAPL/1d@2015-01-01..2025-01-01"
    assert find_item(recipe, key).symbol == "AAPL"
    with pytest.raises(ItemNotFoundError, match="nope"):
        find_item(recipe, "nope")


# --- validate_reproducible (reloj inyectado) -----------------------------------------------


def test_end_within_settle_margin_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "demo-v1", RECIPE)
    recipe = load_recipe(tmp_path, "demo-v1")
    # end máximo = 2025-01-01; con 7 días de margen, hoy debe ser >= 2025-01-08.
    validate_reproducible(recipe, today=date(2025, 1, 8), settle_days=7)
    with pytest.raises(InvalidRecipeError, match="2025-01-01"):
        validate_reproducible(recipe, today=date(2025, 1, 7), settle_days=7)
    validate_reproducible(recipe, today=date(2025, 1, 2), settle_days=1)


# --- canonical_sha256 ----------------------------------------------------------------------


def _sha(directory: Path, text: str) -> str:
    return canonical_sha256(load_recipe(_write(directory, "demo-v1", text).parent, "demo-v1"))


def test_canonical_sha256_invariants(tmp_path: Path) -> None:
    base = _sha(tmp_path / "a", RECIPE)
    same = _sha(tmp_path / "b", REORDERED_WITH_COMMENTS)
    changed = _sha(tmp_path / "c", RECIPE.replace("end = 2024-04-01", "end = 2024-03-01"))
    assert base == same
    assert base != changed


# --- lock ----------------------------------------------------------------------------------


def test_lock_path_and_absent_lock(tmp_path: Path) -> None:
    assert lock_path(tmp_path, "demo-v1") == tmp_path / "demo-v1.lock.json"
    assert load_lock(tmp_path, "demo-v1") is None


def test_write_then_load_round_trip_and_stable_bytes(tmp_path: Path) -> None:
    _write(tmp_path, "demo-v1", RECIPE)
    lock = _lock(tmp_path)
    path = write_lock(tmp_path, lock)
    first = path.read_bytes()
    write_lock(tmp_path, lock)
    assert path.read_bytes() == first
    assert first.endswith(b"}\n")
    assert b"\r\n" not in first
    assert load_lock(tmp_path, "demo-v1") == lock
    data = json.loads(first)
    assert list(data) == sorted(data)  # claves ordenadas
    assert first.decode("utf-8").startswith('{\n  "hash_algorithm"')  # indent=2


def test_failed_replace_keeps_previous_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "demo-v1", RECIPE)
    lock = _lock(tmp_path)
    path = write_lock(tmp_path, lock)
    before = path.read_bytes()

    def boom(self: Path, target: Path) -> Path:
        msg = "replace simulado"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", boom)
    changed = lock.model_copy(update={"tool_versions": {"polars": "9.9.9"}})
    with pytest.raises(OSError, match="replace simulado"):
        write_lock(tmp_path, changed)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo-v1.lock.json", "demo-v1.toml"]


def test_corrupt_lock_rejected(tmp_path: Path) -> None:
    lock_path(tmp_path, "demo-v1").write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidLockError, match="JSON"):
        load_lock(tmp_path, "demo-v1")


def test_other_algorithm_lock_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "demo-v1", RECIPE)
    data = _lock(tmp_path).model_dump(mode="json")
    data["hash_algorithm"] = "ftds-v2"
    lock_path(tmp_path, "demo-v1").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InvalidLockError, match="ftds-v2"):
        load_lock(tmp_path, "demo-v1")
    assert HASH_ALGORITHM == "ftds-v1"


def test_lock_of_other_recipe_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "demo-v1", RECIPE)
    data = _lock(tmp_path).model_dump(mode="json")
    lock_path(tmp_path, "other-v1").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InvalidLockError, match="demo-v1"):
        load_lock(tmp_path, "other-v1")
