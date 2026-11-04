"""B5.1: contrato de motor ``finances_core.backtest.engine_api`` (RF-10, RF-21, ADR-B09).

El motor solo simula: recibe velas, señales ya normalizadas y costes; nunca ``kind``,
``params`` ni la estrategia resuelta.
"""

from __future__ import annotations

import dataclasses
import inspect

import polars as pl
import pytest

from finances_core.backtest.engine_api import BacktestEngine, EngineOutput
from finances_core.backtest.frames import EQUITY_SCHEMA, TRADES_SCHEMA


def test_engine_run_has_no_strategy_parameters() -> None:
    """RF-21: ``BacktestEngine.run`` no recibe ``kind``, ``params`` ni la estrategia."""
    params = list(inspect.signature(BacktestEngine.run).parameters)
    assert params == ["self", "candles", "signals", "costs", "size_fraction", "initial_cash"]
    forbidden = {"kind", "params", "strategy", "resolved", "resolved_strategy"}
    assert forbidden.isdisjoint(params)
    assert not any("strategy" in name or "kind" in name for name in params)


def test_engine_contract_has_no_supported_kinds() -> None:
    """RF-10: el contrato no declara ``supported_kinds`` (no existe ``kind_not_supported``)."""
    members = set(dir(BacktestEngine)) | set(getattr(BacktestEngine, "__annotations__", {}))
    assert "supported_kinds" not in members
    assert {"name", "version", "run"} <= members


def test_engine_output_is_frozen_with_tables_and_warnings() -> None:
    trades = pl.DataFrame(schema=TRADES_SCHEMA)
    equity = pl.DataFrame(schema=EQUITY_SCHEMA)
    out = EngineOutput(trades=trades, equity=equity)
    assert out.warnings == []
    assert [f.name for f in dataclasses.fields(EngineOutput)] == ["trades", "equity", "warnings"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.trades = equity  # type: ignore[misc]
