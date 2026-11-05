"""V1.4: ``VectorbtEngine`` (``name``, ``version()``, ``run``) implementa ``BacktestEngine``.

``engine.run`` sobre el *fixture* con las señales del núcleo de las 3 estrategias
integradas devuelve un ``EngineOutput`` válido; ``version()`` es la de ``vectorbt``
instalado y cae dentro del pin de V0.2 (``engine_version`` de RF-4: ``vectorbt <versión>``).
"""

from __future__ import annotations

import importlib.metadata

import pytest
from finances_core.backtest.engine_api import BacktestEngine, CostModel, EngineOutput
from finances_core.backtest.frames import validate_engine_output

from finances_backtester.ft_vectorbt import ENGINE_NAME
from finances_backtester.ft_vectorbt.engine import VectorbtEngine
from tests.contract.engine_contract import core_signals
from tests.contract.fixture import GOLDEN_COSTS, KINDS, fixture_candles
from tests.ft_vectorbt.test_vectorbt_pin import VERIFIED_PREFIX


def test_name_is_the_engine_name() -> None:
    assert VectorbtEngine.name == ENGINE_NAME == "vectorbt"
    assert VectorbtEngine().name == "vectorbt"


def test_version_is_the_installed_vectorbt_within_the_pin() -> None:
    installed = importlib.metadata.version("vectorbt")
    assert VectorbtEngine().version() == f"vectorbt {installed}"
    assert installed.startswith(VERIFIED_PREFIX)


def test_implements_the_backtest_engine_protocol() -> None:
    engine: BacktestEngine = VectorbtEngine()
    assert callable(engine.run)
    assert callable(engine.version)


@pytest.mark.parametrize("kind", KINDS)
def test_run_on_fixture_with_core_signals_returns_valid_output(kind: str) -> None:
    candles = fixture_candles().to_frame()
    output = VectorbtEngine().run(
        candles,
        core_signals(kind),
        CostModel(fees_bps=GOLDEN_COSTS.fees_bps, slippage_bps=GOLDEN_COSTS.slippage_bps),
        GOLDEN_COSTS.size_fraction,
        GOLDEN_COSTS.initial_cash,
    )
    assert isinstance(output, EngineOutput)
    validate_engine_output(output, candles.height)
    assert output.trades.height > 0
    assert output.warnings == []


def test_run_is_deterministic() -> None:
    candles = fixture_candles().to_frame()
    signals = core_signals("sma_cross")
    costs = CostModel(fees_bps=GOLDEN_COSTS.fees_bps, slippage_bps=GOLDEN_COSTS.slippage_bps)
    first = VectorbtEngine().run(candles, signals, costs, 1.0, 10_000.0)
    second = VectorbtEngine().run(candles, signals, costs, 1.0, 10_000.0)
    assert first.trades.equals(second.trades)
    assert first.equity.equals(second.equity)
