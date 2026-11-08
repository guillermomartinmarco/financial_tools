"""B4.1: identidad de una corrida (``data_hash``, ``run_id``; RF-8, plan §3.3).

Los valores esperados son literales fijados una vez: si cambian, cambia la identidad de
todas las corridas almacenadas y el cambio debe justificarse (``RESULT_SCHEMA_VERSION``).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import struct
from typing import Any

import polars as pl
import pytest

from finances_core.backtest import identity
from finances_core.backtest.catalog import CATALOG_VERSION, RESULT_SCHEMA_VERSION
from finances_core.backtest.identity import data_hash, run_id, run_identity
from finances_core.schemas.backtest import BacktestRequest, ResolvedStrategy

HOUR_MS = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z

#: Vectores fijos (calculados una vez y fijados).
EXPECTED_DATA_HASH = "b1471a386623a2c9957e67c3548fb876bc7c93f0b80f1c511d86de1571ebc6b2"
EXPECTED_RUN_ID = "e91f9714cbaf06f28e197ea375f1d59ab78aedd28eb08b4a3c25f9379baba80a"


def _candles() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open_time_ms": [T0, T0 + HOUR_MS, T0 + 2 * HOUR_MS],
            "open": [100.0, 101.5, 99.25],
            "high": [102.0, 103.0, 100.5],
            "low": [99.5, 100.75, 98.0],
            "close": [101.5, 99.25, 100.0],
            "volume": [10.0, 20.0, 30.0],
        },
        schema={
            "open_time_ms": pl.Int64,
            "open": pl.Float32,
            "high": pl.Float32,
            "low": pl.Float32,
            "close": pl.Float32,
            "volume": pl.Float32,
        },
    )


def _request(**overrides: Any) -> BacktestRequest:
    body: dict[str, Any] = {
        "dataset": {
            "exchange": "binance",
            "market_type": "spot",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
        },
        "strategy_id": "01920000-0000-7000-8000-000000000001",
        "initial_cash": 10_000.0,
        "size_fraction": 1.0,
        "fees_bps": 10.0,
        "slippage_bps": 0.0,
    }
    body.update(overrides)
    return BacktestRequest.model_validate(body)


def _strategy(**overrides: Any) -> ResolvedStrategy:
    body: dict[str, Any] = {
        "strategy_id": "01920000-0000-7000-8000-000000000001",
        "name": "cruce",
        "version": 1,
        "hash": "a" * 64,
        "kind": "sma_cross",
        "params": {"fast": 10, "slow": 30},
        "kind_version": "1.0.0",
        "code_hash": "b" * 64,
        "origin": "builtin",
    }
    body.update(overrides)
    return ResolvedStrategy.model_validate(body)


def _run_id(
    request: BacktestRequest | None = None,
    strategy: ResolvedStrategy | None = None,
    **overrides: Any,
) -> str:
    kwargs: dict[str, Any] = {
        "start_ms": T0,
        "end_ms": T0 + 2 * HOUR_MS,
        "data_hash": "c" * 64,
        "engine": "vectorbt",
        "engine_version": "1.2.0",
    }
    kwargs.update(overrides)
    return run_id(request or _request(), strategy or _strategy(), **kwargs)


# --- data_hash -----------------------------------------------------------------------


def test_data_hash_fixed_vector() -> None:
    assert data_hash(_candles()) == EXPECTED_DATA_HASH


def test_data_hash_is_little_endian_column_bytes_in_order() -> None:
    candles = _candles()
    expected = hashlib.sha256()
    expected.update(struct.pack("<3q", *candles["open_time_ms"].to_list()))
    for column in ("open", "high", "low", "close"):
        expected.update(struct.pack("<3f", *candles[column].to_list()))
    assert data_hash(candles) == expected.hexdigest()


def test_data_hash_ignores_other_columns_and_column_order() -> None:
    candles = _candles()
    reordered = candles.select(["volume", "close", "low", "high", "open", "open_time_ms"])
    changed_volume = candles.with_columns(pl.lit(1.0, dtype=pl.Float32).alias("volume"))
    assert data_hash(reordered) == data_hash(candles)
    assert data_hash(changed_volume) == data_hash(candles)
    assert data_hash(candles.drop("volume")) == data_hash(candles)


@pytest.mark.parametrize("column", ["open_time_ms", "open", "high", "low", "close"])
def test_changing_one_candle_changes_data_hash(column: str) -> None:
    candles = _candles()
    original = candles[column]
    bumped = original.to_list()
    bumped[1] = bumped[1] + 1
    changed = candles.with_columns(pl.Series(column, bumped, dtype=original.dtype))
    assert data_hash(changed) != data_hash(candles)


def test_data_hash_does_not_depend_on_chunking() -> None:
    candles = _candles()
    chunked = pl.concat([candles.head(1), candles.tail(2)], rechunk=False)
    assert chunked.n_chunks() > 1
    assert data_hash(chunked) == data_hash(candles)


def test_data_hash_of_empty_frame_is_sha256_of_nothing() -> None:
    assert data_hash(_candles().head(0)) == hashlib.sha256(b"").hexdigest()


def test_data_hash_rejects_missing_column() -> None:
    with pytest.raises(ValueError, match="close"):
        data_hash(_candles().drop("close"))


@pytest.mark.parametrize(
    ("column", "dtype"),
    [("open_time_ms", pl.UInt64), ("close", pl.Float16), ("open", pl.Int64)],
)
def test_data_hash_rejects_non_canonical_dtype(column: str, dtype: pl.DataType) -> None:
    candles = _candles().with_columns(pl.col(column).cast(dtype))
    with pytest.raises(TypeError, match=column):
        data_hash(candles)


def test_data_hash_rejects_nulls() -> None:
    candles = _candles().with_columns(
        pl.when(pl.int_range(pl.len()) == 1).then(None).otherwise(pl.col("low")).alias("low")
    )
    with pytest.raises(ValueError, match="low"):
        data_hash(candles)


# --- run_id --------------------------------------------------------------------------


def test_run_id_fixed_vector() -> None:
    assert _run_id() == EXPECTED_RUN_ID


def test_run_id_is_sha256_of_canonical_json_of_identity() -> None:
    payload = run_identity(
        _request(),
        _strategy(),
        start_ms=T0,
        end_ms=T0 + 2 * HOUR_MS,
        data_hash="c" * 64,
        engine="vectorbt",
        engine_version="1.2.0",
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert _run_id() == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert set(payload) == {
        "strategy_hash",
        "strategy_id",
        "strategy_version",
        "kind_version",
        "strategy_code_hash",
        "dataset",
        "start_ms",
        "end_ms",
        "data_hash",
        "initial_cash",
        "size_fraction",
        "fees_bps",
        "slippage_bps",
        "engine",
        "engine_version",
        "catalog_version",
        "result_schema_version",
    }
    assert payload["catalog_version"] == CATALOG_VERSION
    assert payload["result_schema_version"] == RESULT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "change",
    [
        {"strategy": {"hash": "d" * 64}},
        {"strategy": {"strategy_id": "01920000-0000-7000-8000-000000000002"}},
        {"strategy": {"version": 2}},
        {"strategy": {"kind_version": "1.0.1"}},
        {"strategy": {"code_hash": "e" * 64}},
        {
            "request": {
                "dataset": {"market_type": "futures", "symbol": "BTCUSDT", "timeframe": "1h"}
            }
        },
        {"request": {"dataset": {"market_type": "spot", "symbol": "ETHUSDT", "timeframe": "1h"}}},
        {"request": {"dataset": {"market_type": "spot", "symbol": "BTCUSDT", "timeframe": "4h"}}},
        {
            "request": {
                "dataset": {
                    "exchange": "yahoo",
                    "market_type": "spot",
                    "symbol": "BTCUSDT",
                    "timeframe": "1h",
                }
            }
        },
        {"kwargs": {"start_ms": T0 + HOUR_MS}},
        {"kwargs": {"end_ms": T0 + HOUR_MS}},
        {"kwargs": {"data_hash": "f" * 64}},
        {"request": {"initial_cash": 5_000.0}},
        {"request": {"size_fraction": 0.5}},
        {"request": {"fees_bps": 5.0}},
        {"request": {"slippage_bps": 1.0}},
        {"kwargs": {"engine": "other_engine"}},
        {"kwargs": {"engine_version": "1.2.1"}},
    ],
    ids=lambda change: next(iter(next(iter(change.values())))),
)
def test_changing_any_rf8_field_changes_run_id(change: dict[str, dict[str, Any]]) -> None:
    request = _request(**change.get("request", {}))
    strategy = _strategy(**change.get("strategy", {}))
    assert _run_id(request, strategy, **change.get("kwargs", {})) != _run_id()


@pytest.mark.parametrize("field", ["catalog_version", "result_schema_version"])
def test_changing_catalog_or_schema_version_changes_run_id(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    baseline = _run_id()
    monkeypatch.setattr(identity, field.upper(), "999")
    assert _run_id() != baseline


def test_changing_one_candle_changes_run_id() -> None:
    candles = _candles()
    changed = candles.with_columns(pl.Series("close", [101.5, 99.5, 100.0], dtype=pl.Float32))
    assert _run_id(data_hash=data_hash(changed)) != _run_id(data_hash=data_hash(candles))


def test_key_order_does_not_influence_run_id() -> None:
    reordered_request = BacktestRequest.model_validate(
        {
            "slippage_bps": 0.0,
            "fees_bps": 10.0,
            "size_fraction": 1.0,
            "initial_cash": 10_000.0,
            "strategy_id": "01920000-0000-7000-8000-000000000001",
            "dataset": {
                "timeframe": "1h",
                "symbol": "BTCUSDT",
                "market_type": "spot",
                "exchange": "binance",
            },
        }
    )
    reordered_strategy = _strategy(params={"slow": 30, "fast": 10})
    assert _run_id(reordered_request, reordered_strategy) == _run_id()


def test_force_does_not_influence_run_id() -> None:
    assert _run_id(_request(force=True)) == _run_id(_request(force=False))


def test_requested_range_does_not_influence_run_id_only_effective_range() -> None:
    requested = _request(start="2024-01-01", end="2024-02-01")
    assert _run_id(requested) == _run_id()


def test_created_at_ms_and_force_are_not_part_of_identity() -> None:
    parameters = inspect.signature(run_id).parameters
    assert "created_at_ms" not in parameters
    assert "force" not in parameters
    payload = run_identity(
        _request(force=True),
        _strategy(),
        start_ms=T0,
        end_ms=T0,
        data_hash="c" * 64,
        engine="vectorbt",
        engine_version="1.2.0",
    )
    assert "created_at_ms" not in payload
    assert "force" not in payload


def test_strategy_fields_outside_rf8_do_not_influence_run_id() -> None:
    # kind y params ya los cubre strategy_hash; name y origin no forman parte de RF-8.
    assert _run_id(strategy=_strategy(name="otro nombre", origin="local")) == _run_id()
