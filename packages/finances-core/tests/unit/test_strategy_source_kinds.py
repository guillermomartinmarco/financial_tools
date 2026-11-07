"""Facade `strategy_source.kinds` / `strategy_source.check_contract` (RF-1, RF-20).

These two additive functions let engine tools (e.g. `ft_vectorbt`) reach the strategy
registry without importing `finances_core.backtest.catalog` or
`finances_core.backtest.strategies`, which RF-21 forbids them. `strategy_source` is the
sanctioned door (it already wraps the catalog internally).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finances_core.backtest import strategy_source
from finances_core.backtest.errors import UnknownKindError

BUILTIN_KINDS = ["donchian_breakout", "rsi_reversion", "sma_cross"]


def _empty_strategies_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "strategies"
    local.mkdir()
    monkeypatch.setenv("FT_STRATEGIES_DIR", str(local))


def test_kinds_lists_builtins_in_deterministic_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _empty_strategies_dir(tmp_path, monkeypatch)
    infos = strategy_source.kinds()
    assert [info.kind for info in infos] == BUILTIN_KINDS
    assert all(info.valid for info in infos)
    assert all(info.origin == "builtin" for info in infos)


def test_check_contract_passes_for_a_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _empty_strategies_dir(tmp_path, monkeypatch)
    strategy_source.check_contract("sma_cross")  # must not raise


def test_check_contract_raises_for_unknown_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _empty_strategies_dir(tmp_path, monkeypatch)
    with pytest.raises(UnknownKindError):
        strategy_source.check_contract("does_not_exist")
