"""B6.1: fixture determinista, referencia independiente y *golden* versionados.

Los casos de 10 barras están calculados a mano (las mismas series que los tests de cada
integrada en ``finances-core``) con ``initial_cash = 1000`` y sin costes salvo donde se
indica; ``open = close`` en cada barra, así que la ejecución en la apertura de ``t+1``
ocurre al cierre de ``t+1``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import polars as pl
import pytest
from finances_core.backtest.frames import validate_engine_output
from finances_core.backtest.metrics import compute
from finances_core.schemas.backtest import RunSummary

from tests.contract.fixture import (
    FIXTURE_BARS,
    GENERATION_MARGIN,
    GOLDEN_COSTS,
    GOLDEN_PARAMS,
    HOUR_MS,
    KINDS,
    SEGMENTS,
    TIE_MARGIN,
    TIMEFRAME,
    build_fixture,
    candles_sha256,
    fixture_candles,
)
from tests.contract.golden_io import GOLDEN_DIR, golden_signals, golden_table, load_json
from tests.contract.reference import (
    Candles,
    Costs,
    equity_frame,
    reference_signals,
    simulate,
    summarize,
    threshold_margin,
    trades_frame,
)
from tests.contract.regenerate_golden import golden_files, write_golden

NO_COSTS = Costs(initial_cash=1000.0, size_fraction=1.0, fees_bps=0.0, slippage_bps=0.0)


def _flat(
    close: list[float], high: list[float] | None = None, low: list[float] | None = None
) -> Candles:
    n = len(close)
    return Candles(
        open_time_ms=tuple(t * HOUR_MS for t in range(n)),
        open=tuple(close),
        high=tuple(high or close),
        low=tuple(low or close),
        close=tuple(close),
    )


def _true(values: list[bool]) -> list[int]:
    return [t for t, value in enumerate(values) if value]


# --------------------------------------------------------------------------- casos a mano


def test_hand_sma_cross_ten_bars() -> None:
    # fast = 2, slow = 3: entry 3, 7, 9 y exit 5, 8 (tabla en test_strategies_sma_cross.py).
    candles = _flat([10.0, 10.0, 10.0, 11.0, 10.0, 8.0, 12.0, 5.0, 10.0, 9.0])
    entries, exits = reference_signals("sma_cross", candles, {"fast": 2, "slow": 3})
    assert (_true(entries), _true(exits)) == ([3, 7, 9], [5, 8])
    sim = simulate(candles, entries, exits, NO_COSTS)
    # entry 3 -> compra open[4] = 10 (100 u.); exit 5 -> vende open[6] = 12: +200.
    # entry 7 -> compra open[8] = 10 (120 u.); exit 8 -> vende open[9] = 9: -120.
    # entry 9 está en la última barra: no se ejecuta.
    rows = [
        (r.entry_time_ms // HOUR_MS, r.exit_time_ms // HOUR_MS, r.size, r.pnl) for r in sim.trades
    ]
    assert rows == [(4, 6, 100.0, 200.0), (8, 9, 120.0, -120.0)]
    assert [r.ret for r in sim.trades] == [0.2, -0.1]
    assert sim.equity == (1000.0,) * 5 + (800.0, 1200.0, 1200.0, 1200.0, 1080.0)
    summary = summarize(sim, candles, initial_cash=1000.0, timeframe="1h")
    assert summary["n_trades"] == 2
    assert summary["win_rate"] == 0.5
    assert summary["exposure"] == pytest.approx(0.3)  # barras 4, 5 y 8
    assert summary["max_drawdown"] == pytest.approx(-0.2)  # 800 / 1000 - 1
    assert summary["total_return"] == pytest.approx(0.08)


def test_hand_rsi_reversion_ten_bars() -> None:
    # period = 2, 30/70: entry 4, 9 y exit 6 (tabla en test_strategies_rsi_reversion.py).
    candles = _flat([10.0, 11.0, 12.0, 11.0, 9.0, 10.0, 13.0, 13.0, 13.0, 10.0])
    params = {"period": 2, "lower": 30.0, "upper": 70.0}
    entries, exits = reference_signals("rsi_reversion", candles, params)
    assert (_true(entries), _true(exits)) == ([4, 9], [6])
    sim = simulate(candles, entries, exits, NO_COSTS)
    # compra open[5] = 10 (100 u.), vende open[7] = 13: +300.
    assert [(r.entry_price, r.exit_price, r.pnl) for r in sim.trades] == [(10.0, 13.0, 300.0)]
    assert sim.equity == (1000.0,) * 6 + (1300.0,) * 4


def test_hand_donchian_breakout_ten_bars() -> None:
    # window = 3: entry 4, 9 y exit 7, 8 (tabla en test_strategies_donchian_breakout.py).
    high = [10.0, 11.0, 12.0, 11.0, 13.0, 12.0, 11.0, 10.0, 9.0, 12.0]
    low = [9.0, 10.0, 11.0, 10.0, 12.0, 11.0, 10.0, 9.0, 8.0, 10.0]
    close = [9.5, 10.5, 11.5, 10.5, 13.0, 11.5, 10.5, 9.5, 8.5, 12.0]
    candles = _flat(close, high, low)
    entries, exits = reference_signals("donchian_breakout", candles, {"window": 3})
    assert (_true(entries), _true(exits)) == ([4, 9], [7, 8])
    sim = simulate(candles, entries, exits, NO_COSTS)
    # compra open[5] = 11.5 (1000/11.5 u.), vende open[8] = 8.5; exit 8 sin posición se ignora.
    (trade,) = sim.trades
    assert (trade.entry_time_ms // HOUR_MS, trade.exit_time_ms // HOUR_MS) == (5, 8)
    assert trade.pnl == pytest.approx(-3000.0 / 11.5)
    assert sim.equity[8] == pytest.approx(1000.0 * 8.5 / 11.5)
    assert sim.exposed_bars == 3


def test_hand_costs_and_end_of_data_liquidation() -> None:
    # open[t] = 100 + t, close[t] = open[t] + 0.5; fees 10 bps y slippage 10 bps.
    n = 10
    candles = Candles(
        open_time_ms=tuple(t * HOUR_MS for t in range(n)),
        open=tuple(100.0 + t for t in range(n)),
        high=tuple(101.0 + t for t in range(n)),
        low=tuple(99.0 + t for t in range(n)),
        close=tuple(100.5 + t for t in range(n)),
    )
    entries = [t in {1, 6, 8} for t in range(n)]
    exits = [t == 4 for t in range(n)]
    costs = Costs(initial_cash=1000.0, size_fraction=1.0, fees_bps=10.0, slippage_bps=10.0)
    first, last = simulate(candles, entries, exits, costs).trades
    # Compra en open[2] = 102: precio 102·1.001, cantidad 1000 / (precio·1.001).
    buy = 102.0 * 1.001
    qty = 1000.0 / (buy * 1.001)
    sell = 105.0 * 0.999  # exit 4 -> open[5]
    fees = 0.001 * buy * qty + 0.001 * sell * qty
    assert first.entry_price == pytest.approx(buy)
    assert first.exit_price == pytest.approx(sell)
    assert first.size == pytest.approx(qty)
    assert first.fees == pytest.approx(fees)
    assert first.pnl == pytest.approx((sell - buy) * qty - fees)
    assert first.ret == pytest.approx(first.pnl / (buy * qty + 0.001 * buy * qty))
    assert first.exit_reason == 0
    # entry 6 -> open[7]; entry 8 con posición se ignora; liquidación al cierre de t = 9.
    assert last.entry_price == pytest.approx(107.0 * 1.001)
    assert last.exit_price == pytest.approx(109.5 * 0.999)
    assert (last.exit_time_ms, last.exit_reason) == (9 * HOUR_MS, 1)


def test_entry_that_would_execute_on_the_last_bar_is_ignored() -> None:
    candles = _flat([10.0, 11.0, 12.0, 13.0])
    sim = simulate(candles, [False, False, True, False], [False] * 4, NO_COSTS)
    assert sim.trades == ()
    assert sim.equity == (1000.0,) * 4


# --------------------------------------------------------------------------- fixture


def test_fixture_is_deterministic_and_pinned() -> None:
    assert build_fixture() == build_fixture()
    assert candles_sha256(fixture_candles()) == load_json("fixture.json")["candles_sha256"]


def test_fixture_shape_is_hourly_ohlc() -> None:
    candles = fixture_candles()
    assert candles.n == FIXTURE_BARS >= 2_000
    times = candles.open_time_ms
    assert all(b - a == HOUR_MS for a, b in zip(times, times[1:], strict=False))
    for t in range(candles.n):
        assert candles.low[t] <= min(candles.open[t], candles.close[t])
        assert candles.high[t] >= max(candles.open[t], candles.close[t])


def test_fixture_alternates_trend_and_range() -> None:
    candles = fixture_candles()
    size = candles.n // SEGMENTS
    moves = [
        candles.close[(k + 1) * size - 1] / candles.close[k * size] - 1 for k in range(SEGMENTS)
    ]
    trends, ranges = moves[0::2], moves[1::2]
    assert [math.copysign(1, m) for m in trends] == [1, -1, 1, -1]
    assert all(abs(m) > 0.2 for m in trends)
    assert all(abs(m) < 0.1 for m in ranges)


@pytest.mark.parametrize("kind", KINDS)
def test_no_threshold_comparison_is_a_tie(kind: str) -> None:
    margin, bar = threshold_margin(kind, fixture_candles(), GOLDEN_PARAMS[kind])
    assert margin >= TIE_MARGIN, f"{kind}: margen {margin} en t={bar}"
    assert GENERATION_MARGIN >= TIE_MARGIN


@pytest.mark.parametrize("kind", KINDS)
def test_each_strategy_trades_at_least_five_times(kind: str) -> None:
    candles = fixture_candles()
    entries, exits = reference_signals(kind, candles, GOLDEN_PARAMS[kind])
    assert len(simulate(candles, entries, exits, GOLDEN_COSTS).trades) >= 5


# --------------------------------------------------------------------------- golden


@pytest.mark.parametrize("kind", KINDS)
def test_reference_reproduces_golden(kind: str) -> None:
    candles = fixture_candles()
    entries, exits = reference_signals(kind, candles, GOLDEN_PARAMS[kind])
    assert (entries, exits) == golden_signals(kind)
    sim = simulate(candles, entries, exits, GOLDEN_COSTS)
    assert trades_frame(sim).equals(golden_table("trades", kind))
    assert equity_frame(candles, sim).equals(golden_table("equity", kind))
    summary = summarize(sim, candles, initial_cash=GOLDEN_COSTS.initial_cash, timeframe=TIMEFRAME)
    assert summary == load_json(f"summary_{kind}.json")


@pytest.mark.parametrize("kind", KINDS)
def test_golden_tables_are_canonical(kind: str) -> None:
    class _Output:
        trades = golden_table("trades", kind)
        equity = golden_table("equity", kind)

    validate_engine_output(_Output(), FIXTURE_BARS)
    assert _Output.trades.height >= 5
    RunSummary.model_validate(load_json(f"summary_{kind}.json"))


@pytest.mark.parametrize("kind", KINDS)
def test_golden_summary_agrees_with_core_metrics(kind: str) -> None:
    # RF-7 de la referencia (doble) frente a metrics.compute (Float32): RFC-0004, 1e-3.
    golden = load_json(f"summary_{kind}.json")
    core = compute(
        golden_table("trades", kind),
        golden_table("equity", kind),
        initial_cash=GOLDEN_COSTS.initial_cash,
        timeframe=TIMEFRAME,
    ).model_dump()
    metrics = golden.keys() - {"start_ms", "end_ms", "warnings"}
    assert metrics <= core.keys()
    for name in sorted(metrics):
        assert core[name] == pytest.approx(golden[name], rel=1e-3, abs=1e-6), name


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    first = {p.name: p.read_bytes() for p in write_golden(tmp_path / "a")}
    second = {p.name: p.read_bytes() for p in write_golden(tmp_path / "b")}
    assert sorted(first) == sorted(golden_files())
    assert first == second
    for name, content in first.items():
        versioned = GOLDEN_DIR / name
        if name.endswith(".json"):
            assert content == versioned.read_bytes(), f"{name}: regenerar los golden"
        else:
            assert pl.read_parquet(tmp_path / "a" / name).equals(pl.read_parquet(versioned))


def test_golden_files_are_lf_json() -> None:
    for name in golden_files():
        if name.endswith(".json"):
            raw = (GOLDEN_DIR / name).read_bytes()
            assert b"\r" not in raw
            json.loads(raw)
