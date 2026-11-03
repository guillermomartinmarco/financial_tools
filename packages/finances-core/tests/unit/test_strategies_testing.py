"""B1.6: ``assert_strategy_contract`` y anti look-ahead (RF-20, plan §3.8).

Las estrategias de prueba son carpetas locales temporales registradas con
``build_registry``; ninguna es integrada.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest
from backtest_helpers import VALID_LOGIC, write_strategy

from finances_core.backtest.catalog import StrategyRegistry, build_registry
from finances_core.backtest.errors import UnknownKindError
from finances_core.backtest.strategies.testing import (
    SYNTHETIC_BARS,
    StrategyContractError,
    assert_strategy_contract,
    lookahead_cuts,
    synthetic_candles,
)

SIGNALS_BODY = """\
        close = candles["close"]
        past = close.shift(params.window)
        return Signals(entries=close > past, exits=close < past)
"""


def _logic(body: str, prelude: str = "", warmup: str = "params.window") -> str:
    logic = VALID_LOGIC.replace(SIGNALS_BODY, body).replace(
        "        return params.window\n", f"        return {warmup}\n"
    )
    return logic.replace("\n\nclass MomentumParams", f"\n{prelude}\n\nclass MomentumParams")


def _registry(tmp_path: Path, **folders: str) -> StrategyRegistry:
    local = tmp_path / "local"
    write_strategy(local, "valid_kind")
    for name, logic in folders.items():
        write_strategy(local, name, logic=logic)
    return build_registry(builtin_dir=tmp_path / "no_builtins", local_dir=local)


# ------------------------------------------------------------------- serie sintética
def test_synthetic_candles_are_deterministic_and_canonical() -> None:
    first = synthetic_candles()
    assert first.equals(synthetic_candles())
    assert first.height == SYNTHETIC_BARS == 600
    assert first.schema == pl.Schema(
        {
            "open_time_ms": pl.Int64,
            "open": pl.Float32,
            "high": pl.Float32,
            "low": pl.Float32,
            "close": pl.Float32,
        }
    )
    assert first["open_time_ms"].is_sorted()
    assert first["open_time_ms"].n_unique() == first.height
    assert (first["low"] > 0).all()
    assert (first["high"] >= first["close"]).all()
    assert (first["low"] <= first["close"]).all()
    assert not synthetic_candles(seed=7).equals(first)


def test_synthetic_candles_have_trend_and_range() -> None:
    close = synthetic_candles()["close"]
    quarter = close.len() // 4
    assert close[quarter - 1] > close[0] * 1.1  # tendencia alcista
    assert close[3 * quarter - 1] < close[2 * quarter - 1] * 0.9  # tendencia bajista


# ------------------------------------------------------------------------- cortes
def test_lookahead_cuts_include_required_points() -> None:
    assert lookahead_cuts(5, 600) == [5, 6, 150, 300, 450, 599]


def test_lookahead_cuts_discard_invalid_points() -> None:
    assert lookahead_cuts(0, 8) == [1, 2, 4, 6, 7]
    assert lookahead_cuts(700, 600) == [150, 300, 450, 599]
    assert lookahead_cuts(3, 1) == []


# ---------------------------------------------------------------------- contrato
def test_valid_synthetic_strategy_passes(tmp_path: Path) -> None:
    assert_strategy_contract("valid_kind", _registry(tmp_path))


def test_lookahead_with_shift_minus_one_fails(tmp_path: Path) -> None:
    body = (
        '        close = candles["close"]\n'
        "        future = close.shift(-1)\n"
        "        up = (future > close).fill_null(False)\n"
        "        return Signals(entries=up, exits=(future < close).fill_null(False))\n"
    )
    registry = _registry(tmp_path, peek_kind=_logic(body))
    with pytest.raises(StrategyContractError, match="anticipación") as exc:
        assert_strategy_contract("peek_kind", registry)
    assert exc.value.check == 4


def test_deterministic_lookahead_is_detected_at_every_cut(tmp_path: Path) -> None:
    body = (
        '        nxt = candles["close"].shift(-1).is_not_null()\n'
        "        return Signals(entries=nxt, exits=nxt & False)\n"
    )
    with pytest.raises(StrategyContractError, match=r"k=4: .*t=3") as exc:
        assert_strategy_contract("next_kind", _registry(tmp_path, next_kind=_logic(body)))
    assert exc.value.check == 4


def test_wrong_dtype_fails(tmp_path: Path) -> None:
    body = (
        '        close = candles["close"]\n'
        "        flags = (close > close.shift(1)).fill_null(False).cast(pl.Int8)\n"
        "        return Signals(entries=flags, exits=flags)\n"
    )
    with pytest.raises(StrategyContractError, match="Boolean") as exc:
        assert_strategy_contract("int_kind", _registry(tmp_path, int_kind=_logic(body)))
    assert exc.value.check == 3


def test_nulls_after_warmup_fail(tmp_path: Path) -> None:
    body = (
        '        close = candles["close"]\n'
        "        past = close.shift(params.window + 5)\n"
        "        return Signals(entries=close > past, exits=close < past)\n"
    )
    with pytest.raises(StrategyContractError, match="nulos") as exc:
        assert_strategy_contract("null_kind", _registry(tmp_path, null_kind=_logic(body)))
    assert exc.value.check == 3


def test_mutating_candles_fails(tmp_path: Path) -> None:
    body = (
        '        close = candles["close"]\n'
        '        candles.drop_in_place("open")\n'
        "        past = close.shift(params.window)\n"
        "        return Signals(entries=close > past, exits=close < past)\n"
    )
    with pytest.raises(StrategyContractError, match="muta") as exc:
        assert_strategy_contract("mut_kind", _registry(tmp_path, mut_kind=_logic(body)))
    assert exc.value.check == 3


def test_non_deterministic_strategy_fails(tmp_path: Path) -> None:
    body = (
        "        _CALLS.append(1)\n"
        '        close = candles["close"]\n'
        "        flip = len(_CALLS) % 2 == 0\n"
        "        past = close.shift(params.window)\n"
        "        cond = (close > past) if flip else (close < past)\n"
        "        return Signals(entries=cond, exits=~cond)\n"
    )
    registry = _registry(tmp_path, rand_kind=_logic(body, prelude="_CALLS: list[int] = []"))
    with pytest.raises(StrategyContractError, match="determinista") as exc:
        assert_strategy_contract("rand_kind", registry)
    assert exc.value.check == 3


def test_exception_inside_signals_fails(tmp_path: Path) -> None:
    body = '        raise ValueError("boom")\n'
    with pytest.raises(StrategyContractError, match="ValueError: boom") as exc:
        assert_strategy_contract("boom_kind", _registry(tmp_path, boom_kind=_logic(body)))
    assert exc.value.check == 3


def test_every_valid_cut_is_evaluated(tmp_path: Path) -> None:
    body = (
        "        SEEN.append(candles.height)\n"
        '        close = candles["close"]\n'
        "        past = close.shift(params.window)\n"
        "        return Signals(entries=close > past, exits=close < past)\n"
    )
    registry = _registry(tmp_path, spy_kind=_logic(body, prelude="SEEN: list[int] = []"))
    assert_strategy_contract("spy_kind", registry)
    seen = sys.modules[registry.get("spy_kind").__module__].SEEN
    assert set(seen) == {SYNTHETIC_BARS, *lookahead_cuts(3, SYNTHETIC_BARS)}


def test_warmup_beyond_series_discards_cuts_without_error(tmp_path: Path) -> None:
    body = (
        "        SEEN.append(candles.height)\n"
        '        close = candles["close"]\n'
        "        past = close.shift(params.window)\n"
        "        return Signals(entries=close > past, exits=close < past)\n"
    )
    logic = _logic(body, prelude="SEEN: list[int] = []", warmup="10_000")
    registry = _registry(tmp_path, long_kind=logic)
    assert_strategy_contract("long_kind", registry)
    seen = sys.modules[registry.get("long_kind").__module__].SEEN
    assert set(seen) == {SYNTHETIC_BARS, 150, 300, 450, 599}


def test_check_params_are_also_verified(tmp_path: Path) -> None:
    logic = VALID_LOGIC.replace(
        '    default_params = {{"window": 3}}\n',
        '    default_params = {{"window": 3}}\n    check_params = ({{"window": 50}},)\n',
    ).replace(
        SIGNALS_BODY,
        SIGNALS_BODY.replace(
            "        past = close.shift(params.window)\n",
            "        past = close.shift(params.window)\n"
            "        if params.window == 50:\n"
            "            past = close.shift(-1)\n",
        ),
    )
    with pytest.raises(StrategyContractError, match="window=50") as exc:
        assert_strategy_contract("multi_kind", _registry(tmp_path, multi_kind=logic))
    assert exc.value.check == 3


# ------------------------------------------------------------ estructura y AST
def test_structure_is_rechecked(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    (tmp_path / "local" / "valid_kind" / "README.md").unlink()
    with pytest.raises(StrategyContractError, match="README.md") as exc:
        assert_strategy_contract("valid_kind", registry)
    assert exc.value.check == 1


def test_ast_guard_is_rechecked(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    (tmp_path / "local" / "valid_kind" / "extra.py").write_text("import os\n", encoding="utf-8")
    with pytest.raises(StrategyContractError, match="extra.py") as exc:
        assert_strategy_contract("valid_kind", registry)
    assert exc.value.check == 2


def test_invalid_local_strategy_fails_structure_check(tmp_path: Path) -> None:
    registry = _registry(tmp_path, bad_kind="x = 1\n")
    with pytest.raises(StrategyContractError, match="subclase concreta") as exc:
        assert_strategy_contract("bad_kind", registry)
    assert exc.value.check == 1


def test_unknown_kind_is_reported(tmp_path: Path) -> None:
    with pytest.raises(UnknownKindError):
        assert_strategy_contract("missing_kind", _registry(tmp_path))


def test_default_registry_uses_ft_strategies_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_strategy(tmp_path / "env_dir", "env_kind")
    monkeypatch.setenv("FT_STRATEGIES_DIR", str(tmp_path / "env_dir"))
    assert_strategy_contract("env_kind")


def test_system_exit_inside_signals_fails_the_check(tmp_path: Path) -> None:
    body = "        raise SystemExit(7)\n"
    with pytest.raises(StrategyContractError, match="SystemExit") as exc:
        assert_strategy_contract("exit_kind", _registry(tmp_path, exit_kind=_logic(body)))
    assert exc.value.check == 3
