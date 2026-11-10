"""V1.3: el motor simula señales arbitrarias sin conocer la estrategia (RF-1; RF-21).

- ``simulate`` y ``VectorbtEngine.run`` con las señales de la estrategia sintética de la
  suite de contrato y con señales aleatorias sembradas dan ``trades``/``equity`` válidos
  (y, para las aleatorias, iguales a la referencia independiente de la suite).
- La firma de ``run`` no tiene ``kind``/``params``/estrategia.
- ``engine.py`` no define ``KINDS_IMPL`` ni ``supported_kinds`` ni calcula medias, RSI o
  canales.
"""

from __future__ import annotations

import ast
import inspect
import random
from pathlib import Path

import polars as pl
import pytest
from finances_core.backtest.engine_api import CostModel, Signals
from finances_core.backtest.frames import validate_engine_output

from finances_backtester.ft_vectorbt import engine as engine_module
from finances_backtester.ft_vectorbt.engine import (
    VectorbtEngine,
    from_framework_output,
    simulate,
    to_framework_input,
)
from tests.contract.engine_contract import SYNTHETIC_KIND, assert_tables_match, core_signals
from tests.contract.fixture import GOLDEN_COSTS, fixture_candles
from tests.contract.reference import Costs, equity_frame, trades_frame
from tests.contract.reference import simulate as reference_simulate

ENGINE_SOURCE = Path(inspect.getfile(engine_module))
COSTS = CostModel(fees_bps=GOLDEN_COSTS.fees_bps, slippage_bps=GOLDEN_COSTS.slippage_bps)
#: Identificadores que delatarían señales o indicadores dentro del motor (RF-1).
FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "KINDS_IMPL",
        "supported_kinds",
        "kind",
        "params",
        "strategy",
        "rolling",
        "rolling_mean",
        "ewm",
        "mean",
        "MA",
        "RSI",
        "BBANDS",
        "MSTD",
        "ATR",
        "IndicatorFactory",
    }
)
#: Palabras (separadas por ``_``, en minúsculas) de identificadores de indicadores.
FORBIDDEN_WORDS = frozenset(
    {"sma", "ema", "rsi", "donchian", "channel", "indicator", "indicators", "wilder"}
)


def _random_signals(n: int, seed: int) -> Signals:
    """Señales normalizadas aleatorias (sin ``entry`` y ``exit`` a la vez, RF-16)."""
    rng = random.Random(seed)
    entries, exits = [], []
    for _ in range(n):
        draw = rng.random()
        entries.append(draw < 0.15)
        exits.append(0.15 <= draw < 0.30)
    empty = pl.Series([False] * n, dtype=pl.Boolean)
    return Signals(
        entries=pl.Series("entries", entries, dtype=pl.Boolean),
        exits=pl.Series("exits", exits, dtype=pl.Boolean),
        entries_short=empty.rename("entries_short"),
        exits_short=empty.rename("exits_short"),
    )


def _reference(signals: Signals, size_fraction: float) -> tuple[pl.DataFrame, pl.DataFrame]:
    candles = fixture_candles()
    sim = reference_simulate(
        candles,
        signals.entries.to_list(),
        signals.exits.to_list(),
        Costs(
            initial_cash=GOLDEN_COSTS.initial_cash,
            size_fraction=size_fraction,
            fees_bps=GOLDEN_COSTS.fees_bps,
            slippage_bps=GOLDEN_COSTS.slippage_bps,
        ),
    )
    return trades_frame(sim), equity_frame(candles, sim)


def test_synthetic_contract_strategy_signals_through_simulate_and_run() -> None:
    candles = fixture_candles().to_frame()
    signals = core_signals(SYNTHETIC_KIND)

    res = simulate(to_framework_input(candles, signals), COSTS, 1.0, GOLDEN_COSTS.initial_cash)
    via_simulate = from_framework_output(res, candles["open_time_ms"])
    via_run = VectorbtEngine().run(candles, signals, COSTS, 1.0, GOLDEN_COSTS.initial_cash)

    for out in (via_simulate, via_run):
        validate_engine_output(out, candles.height)
        assert out.trades.height > 0
    assert via_run.trades.equals(via_simulate.trades)
    assert via_run.equity.equals(via_simulate.equity)


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 2026])
@pytest.mark.parametrize("size_fraction", [1.0, 0.35])
def test_seeded_random_signals_match_the_independent_reference(
    seed: int, size_fraction: float
) -> None:
    candles = fixture_candles().to_frame()
    signals = _random_signals(candles.height, seed)

    out = VectorbtEngine().run(candles, signals, COSTS, size_fraction, GOLDEN_COSTS.initial_cash)

    validate_engine_output(out, candles.height)
    trades, equity = _reference(signals, size_fraction)
    assert out.trades.height > 0
    assert_tables_match("trades", out.trades, trades, label=f"[random/{seed}]")
    assert_tables_match("equity", out.equity, equity, label=f"[random/{seed}]")


def test_run_signature_has_no_kind_params_or_strategy() -> None:
    params = list(inspect.signature(VectorbtEngine.run).parameters)
    # Enmienda G4: ``risk`` es el último parámetro, opcional y aditivo.
    assert params == [
        "self",
        "candles",
        "signals",
        "costs",
        "size_fraction",
        "initial_cash",
        "risk",
    ]


def test_engine_has_no_kind_registry_nor_indicator_math() -> None:
    tree = ast.parse(ENGINE_SOURCE.read_text(encoding="utf-8"))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.ClassDef):
            identifiers.add(node.name)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    assert identifiers & FORBIDDEN_IDENTIFIERS == set()
    assert [i for i in identifiers if FORBIDDEN_WORDS & set(i.lower().split("_"))] == []
    assert not hasattr(VectorbtEngine, "supported_kinds")
    assert not hasattr(engine_module, "KINDS_IMPL")
