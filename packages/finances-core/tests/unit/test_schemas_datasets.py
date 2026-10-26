"""T1.1 y T1.2 de ``ft_datasets``: contratos de receta, *lock*, verificación y tarea."""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.datasets import (
    HASH_ALGORITHM,
    BlockRun,
    DatasetRecipe,
    DownloadBlock,
    ItemLock,
    ItemVerification,
    LockResult,
    MaterializePlan,
    MaterializeProgress,
    MaterializeResult,
    Probe,
    RecipeBlock,
    RecipeItem,
    RecipeLock,
    VerifyReport,
)

BENCHMARK_V1 = """
schema_version = 1
name = "benchmark-v1"
description = "Datos reales para tests masivos: cripto 24/7 intradía y multi-activo diario."

[[datasets]]                       # 2 × 2 ítems
exchange = "binance"
market_type = "spot"
symbols = ["BTCUSDT", "ETHUSDT"]
timeframes = ["1m", "1h"]
start = 2024-01-01
end = 2024-04-01                   # exclusivo

[[datasets]]
exchange = "yfinance"
market_type = "equity"
symbols = ["AAPL"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01

[[datasets]]
exchange = "yfinance"
market_type = "index"
symbols = ["^GSPC"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01

[[datasets]]
exchange = "yfinance"
market_type = "fx"
symbols = ["EURUSD=X"]
timeframes = ["1d"]
start = 2015-01-01
end = 2025-01-01
"""

BENCHMARK_KEYS = [
    "binance/spot/BTCUSDT/1m@2024-01-01..2024-04-01",
    "binance/spot/BTCUSDT/1h@2024-01-01..2024-04-01",
    "binance/spot/ETHUSDT/1m@2024-01-01..2024-04-01",
    "binance/spot/ETHUSDT/1h@2024-01-01..2024-04-01",
    "yfinance/equity/AAPL/1d@2015-01-01..2025-01-01",
    "yfinance/index/^GSPC/1d@2015-01-01..2025-01-01",
    "yfinance/fx/EURUSD=X/1d@2015-01-01..2025-01-01",
]

JAN_2024_MS = 1_704_067_200_000
APR_2024_MS = 1_711_929_600_000


def _benchmark() -> dict[str, Any]:
    return tomllib.loads(BENCHMARK_V1)


def _block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "exchange": "binance",
        "market_type": "spot",
        "symbols": ["BTCUSDT"],
        "timeframes": ["1h"],
        "start": date(2024, 1, 1),
        "end": date(2024, 4, 1),
    }
    block.update(overrides)
    return block


def _recipe(*blocks: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "name": "demo-v1",
        "datasets": list(blocks) or [_block()],
    }
    data.update(overrides)
    return data


# --- T1.1: receta ------------------------------------------------------------------------


def test_benchmark_v1_validates_and_expands_to_seven_stable_items() -> None:
    recipe = DatasetRecipe.model_validate(_benchmark())
    items = recipe.items()
    assert [item.key for item in items] == BENCHMARK_KEYS
    assert [item.key for item in DatasetRecipe.model_validate(_benchmark()).items()] == (
        BENCHMARK_KEYS
    )
    first = items[0]
    assert (first.start_ms, first.end_ms) == (JAN_2024_MS, APR_2024_MS)
    assert first.dataset() == DatasetIdentifier(
        exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1m"
    )


def test_item_key_format_and_frozen() -> None:
    item = RecipeItem(
        exchange="yfinance",
        market_type="equity",
        symbol="AAPL",
        timeframe="1d",
        start_ms=JAN_2024_MS,
        end_ms=APR_2024_MS,
    )
    assert item.key == "yfinance/equity/AAPL/1d@2024-01-01..2024-04-01"
    attribute = "symbol"
    with pytest.raises(ValidationError):
        setattr(item, attribute, "MSFT")


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        (_block(exchange="kraken"), "exchange"),
        (_block(market_type="equity"), "market_type"),
        (_block(exchange="yfinance", market_type="spot", timeframes=["1d"]), "market_type"),
        (_block(exchange="yfinance", market_type="other", timeframes=["1d"]), "market_type"),
        (_block(exchange="yfinance", market_type="equity", timeframes=["1h"]), "1h"),
        (_block(timeframes=["2d"]), "2d"),
        (_block(start=date(2024, 4, 1), end=date(2024, 4, 1)), "start"),
        (_block(start=date(2024, 5, 1)), "start"),
        (_block(symbols=["BTCUSDT", "BTCUSDT"]), "duplicad"),
        (_block(timeframes=["1h", "1h"]), "duplicad"),
        (_block(symbols=[]), "symbols"),
        (_block(timeframes=[]), "timeframes"),
        (_block(symbols=["../etc"]), "symbol"),
        (_block(end="now"), "end"),
        (_block(end=1_711_929_600), "end"),
        (_block(end=datetime(2024, 4, 1, tzinfo=UTC)), "end"),
        (_block(foo=1), "foo"),
    ],
)
def test_invalid_blocks_rejected_with_message(block: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        DatasetRecipe.model_validate(_recipe(block))
    assert fragment in str(exc_info.value)


def test_end_absent_rejected() -> None:
    block = _block()
    del block["end"]
    with pytest.raises(ValidationError, match="end"):
        DatasetRecipe.model_validate(_recipe(block))


def test_iso_string_dates_accepted() -> None:
    recipe = DatasetRecipe.model_validate(_recipe(_block(start="2024-01-01", end="2024-04-01")))
    assert recipe.datasets[0].start == date(2024, 1, 1)


def test_duplicate_item_keys_across_blocks_rejected() -> None:
    a = _block(symbols=["BTCUSDT", "ETHUSDT"])
    b = _block(symbols=["ETHUSDT"])
    with pytest.raises(ValidationError, match=r"binance/spot/ETHUSDT/1h@2024-01-01\.\.2024-04-01"):
        DatasetRecipe.model_validate(_recipe(a, b))


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"name": "Bad Name"}, "name"),
        ({"name": "../x"}, "name"),
        ({"name": "-x"}, "name"),
        ({"extra_field": True}, "extra_field"),
        ({"datasets": []}, "datasets"),
    ],
)
def test_invalid_recipe_rejected_with_message(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        DatasetRecipe.model_validate(_recipe(**overrides))
    assert fragment in str(exc_info.value)


def test_recipe_block_is_frozen() -> None:
    block = RecipeBlock.model_validate(_block())
    attribute = "exchange"
    with pytest.raises(ValidationError):
        setattr(block, attribute, "yfinance")


def test_canonical_sha256_format() -> None:
    digest = DatasetRecipe.model_validate(_benchmark()).canonical_sha256()
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


# --- T1.2: lock, verificación y tarea ----------------------------------------------------


def _probe(row: int = 0) -> Probe:
    return Probe(row=row, open_time_ms=JAN_2024_MS, close_bits="0x42f6e979", close=123.456)


def _item_lock(**overrides: Any) -> ItemLock:
    data: dict[str, Any] = {
        "key": "binance/spot/BTCUSDT/1h@2024-01-01..2024-04-01",
        "exchange": "binance",
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "start_ms": JAN_2024_MS,
        "end_ms": APR_2024_MS,
        "row_count": 2184,
        "first_open_time_ms": JAN_2024_MS,
        "last_open_time_ms": APR_2024_MS - 3_600_000,
        "strict_columns": ["open_time_ms", "close"],
        "content_hash": "sha256:" + "a" * 64,
        "column_hashes": {"open_time_ms": "sha256:" + "b" * 64, "close": "sha256:" + "c" * 64},
        "month_hashes": {"2024-01": "sha256:" + "d" * 64},
        "informational": {},
        "probes": [_probe(0), _probe(1092), _probe(2183)],
        "missing_candles": 0,
    }
    data.update(overrides)
    return ItemLock.model_validate(data)


def _recipe_lock(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "lock_version": 1,
        "hash_algorithm": HASH_ALGORITHM,
        "recipe": "demo-v1",
        "recipe_sha256": "sha256:" + "e" * 64,
        "locked_at": "2026-10-01T12:00:00Z",
        "tool_versions": {"polars": "1.6.0"},
        "items": [_item_lock().model_dump(mode="json")],
    }
    data.update(overrides)
    return data


def _verification(**overrides: Any) -> ItemVerification:
    data: dict[str, Any] = {
        "key": "binance/spot/BTCUSDT/1h@2024-01-01..2024-04-01",
        "status": "OK",
        "row_count": 2184,
        "expected_row_count": 2184,
        "content_hash": "sha256:" + "a" * 64,
        "expected_content_hash": "sha256:" + "a" * 64,
    }
    data.update(overrides)
    return ItemVerification.model_validate(data)


def _report() -> VerifyReport:
    return VerifyReport(
        recipe="demo-v1",
        status="MISMATCH",
        recipe_changed=False,
        items=[
            _verification(),
            _verification(
                key="yfinance/equity/AAPL/1d@2015-01-01..2025-01-01",
                status="MISMATCH",
                drift="SCALE_RESTATED",
                scale_factor=0.25,
                columns_changed=["open", "high", "low", "close"],
                months_changed=["2020-08", "2020-09"],
                action="Re-bloquear con `lock --update`",
            ),
            _verification(
                key="yfinance/fx/EURUSD=X/1d@2015-01-01..2025-01-01",
                status="MISSING",
                row_count=0,
                content_hash=None,
            ),
        ],
        duration_ms=12,
    )


def _task_models() -> list[BaseModel]:
    block = DownloadBlock(
        exchange="binance",
        market_type="spot",
        symbols=["BTCUSDT"],
        timeframes=["1m", "1h"],
        start_ms=JAN_2024_MS,
        end_ms_inclusive=APR_2024_MS - 1,
        argv=["ft_market_data_binance", "download", "-s", "BTCUSDT"],
    )
    run = BlockRun(
        block=0,
        exit_code=0,
        duration_ms=1500,
        timed_out=False,
        stderr_tail=["line"],
        downloader_task_id="0192f0c4-0000-7000-8000-000000000000",
    )
    return [
        block,
        run,
        MaterializePlan(
            task_id="t",
            created_at_ms=1,
            recipe="demo-v1",
            skipped_items=[],
            refetch_items=[],
            blocks=[block],
        ),
        MaterializeProgress(
            task_id="t",
            status="DOWNLOADING",
            recipe="demo-v1",
            items_total=7,
            items_skipped=3,
            blocks_total=2,
            blocks_done=1,
            errors=[],
        ),
        MaterializeResult(
            task_id="t",
            status="COMPLETED",
            outcome="MISMATCH",
            recipe="demo-v1",
            blocks=[run],
            verify=_report(),
            errors=[],
        ),
        LockResult(recipe="demo-v1", outcome="REJECTED", lock_path="datasets/x", differences=["a"]),
        _report(),
        _item_lock(),
        RecipeLock.model_validate(_recipe_lock()),
    ]


@pytest.mark.parametrize("model", _task_models(), ids=lambda m: type(m).__name__)
def test_json_round_trip(model: BaseModel) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


def test_recipe_lock_rejects_other_algorithm_and_version() -> None:
    with pytest.raises(ValidationError, match="hash_algorithm"):
        RecipeLock.model_validate(_recipe_lock(hash_algorithm="ftds-v2"))
    with pytest.raises(ValidationError, match="lock_version"):
        RecipeLock.model_validate(_recipe_lock(lock_version=2))


def test_item_lock_requires_rows_and_rejects_extra() -> None:
    with pytest.raises(ValidationError, match="row_count"):
        _item_lock(row_count=0)
    with pytest.raises(ValidationError, match="surprise"):
        _item_lock(surprise=1)


def test_probe_bits_format() -> None:
    with pytest.raises(ValidationError, match="close_bits"):
        Probe(row=0, open_time_ms=0, close_bits="42", close=1.0)
    assert Probe(row=0, open_time_ms=0, close_bits=None, close=None).close is None


def test_to_markdown_one_row_per_item_with_status_and_drift() -> None:
    markdown = _report().to_markdown()
    rows = [line for line in markdown.splitlines() if line.startswith("| `")]
    assert len(rows) == 3
    assert "demo-v1" in markdown
    assert "MISMATCH" in markdown
    assert "OK" in rows[0]
    assert "SCALE_RESTATED" in rows[1]
    assert "0.25" in rows[1]
    assert "2020-08" in rows[1]
    assert "MISSING" in rows[2]
