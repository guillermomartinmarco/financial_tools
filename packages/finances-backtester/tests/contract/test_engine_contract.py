"""B6.2: tests de la propia suite de contrato de motor (``engine_contract``).

La referencia independiente (``reference.simulate``) se envuelve como motor *fake*: solo
recibe velas, señales y costes, como cualquier ``BacktestEngine`` (RF-21). Cada mutación
es un motor *fake* que altera una sola cosa y la suite debe rechazarla con un
``AssertionError`` que nombre el campo culpable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.backtest.engine_api import EngineOutput
from finances_core.backtest.frames import EXIT_REASON_END_OF_DATA, EXIT_REASON_SIGNAL
from finances_core.backtest.strategies.base import Signals
from finances_core.schemas.backtest import CostModel

from tests.contract.engine_contract import (
    CONTRACT_KINDS,
    ENGINE_SOURCE_ROOTS,
    SYNTHETIC_KIND,
    assert_engine_matches_golden,
    assert_engine_modules_isolated,
    assert_tables_match,
    core_signals,
    engine_import_violations,
)
from tests.contract.fixture import HOUR_MS, KINDS, fixture_candles
from tests.contract.golden_io import golden_table
from tests.contract.reference import Candles, Costs, equity_frame, simulate, trades_frame


def _to_candles(frame: pl.DataFrame) -> Candles:
    return Candles(
        open_time_ms=tuple(frame["open_time_ms"].to_list()),
        open=tuple(frame["open"].to_list()),
        high=tuple(frame["high"].to_list()),
        low=tuple(frame["low"].to_list()),
        close=tuple(frame["close"].to_list()),
    )


class ReferenceEngine:
    """``reference.simulate`` envuelto como ``BacktestEngine``: sin ``kind`` ni ``params``."""

    name = "reference"

    def __init__(self, *, fee_bps_delta: float = 0.0) -> None:
        self.fee_bps_delta = fee_bps_delta
        self.calls = 0

    def version(self) -> str:
        return "reference 1.0.0"

    def run(
        self,
        candles: pl.DataFrame,
        signals: Signals,
        costs: CostModel,
        size_fraction: float,
        initial_cash: float,
    ) -> EngineOutput:
        self.calls += 1
        bars = _to_candles(candles)
        sim = simulate(
            bars,
            signals.entries.to_list(),
            signals.exits.to_list(),
            Costs(
                initial_cash=initial_cash,
                size_fraction=size_fraction,
                fees_bps=costs.fees_bps + self.fee_bps_delta,
                slippage_bps=costs.slippage_bps,
            ),
        )
        return EngineOutput(trades=trades_frame(sim), equity=equity_frame(bars, sim))


class MutatedEngine(ReferenceEngine):
    """Motor de referencia cuya tabla ``trades`` se altera tras simular."""

    def __init__(self, mutate: Any) -> None:
        super().__init__()
        self.mutate = mutate

    def run(
        self,
        candles: pl.DataFrame,
        signals: Signals,
        costs: CostModel,
        size_fraction: float,
        initial_cash: float,
    ) -> EngineOutput:
        out = super().run(candles, signals, costs, size_fraction, initial_cash)
        return EngineOutput(trades=self.mutate(out.trades), equity=out.equity)


def _first_row(column: str, value: pl.Expr) -> Any:
    def mutate(trades: pl.DataFrame) -> pl.DataFrame:
        row = pl.int_range(pl.len()) == 0
        return trades.with_columns(
            pl.when(row).then(value).otherwise(pl.col(column)).alias(column)
        ).cast(trades.schema)

    return mutate


# --------------------------------------------------------------------------- pasa


@pytest.mark.parametrize("kind", CONTRACT_KINDS)
def test_reference_engine_passes_the_suite(kind: str) -> None:
    engine = ReferenceEngine()
    assert_engine_matches_golden(engine, kind)
    assert engine.calls == 1


def test_contract_kinds_are_the_builtins_plus_synthetic() -> None:
    assert (*KINDS, SYNTHETIC_KIND) == CONTRACT_KINDS


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="no_such_kind"):
        assert_engine_matches_golden(ReferenceEngine(), "no_such_kind")


def test_signals_come_from_the_core_not_the_engine() -> None:
    # El motor recibe exactamente las señales del núcleo (registro + finalize).
    seen: list[Signals] = []

    class Spy(ReferenceEngine):
        def run(self, candles: pl.DataFrame, signals: Signals, *args: Any) -> EngineOutput:
            seen.append(signals)
            return super().run(candles, signals, *args)

    assert_engine_matches_golden(Spy(), "sma_cross")
    expected = core_signals("sma_cross")
    assert seen[0].entries.equals(expected.entries)
    assert seen[0].exits.equals(expected.exits)


# --------------------------------------------------------------------------- mutaciones


@pytest.mark.parametrize("kind", CONTRACT_KINDS)
def test_entry_time_shifted_one_bar_fails(kind: str) -> None:
    engine = MutatedEngine(_first_row("entry_time_ms", pl.col("entry_time_ms") + HOUR_MS))
    with pytest.raises(AssertionError, match=r"trades\.entry_time_ms.*fila 0"):
        assert_engine_matches_golden(engine, kind)


@pytest.mark.parametrize("kind", ["rsi_reversion", SYNTHETIC_KIND])
def test_missing_final_liquidation_fails(kind: str) -> None:
    def drop_end_of_data(trades: pl.DataFrame) -> pl.DataFrame:
        return trades.filter(pl.col("exit_reason") != EXIT_REASON_END_OF_DATA)

    with pytest.raises(AssertionError, match="n_trades"):
        assert_engine_matches_golden(MutatedEngine(drop_end_of_data), kind)


@pytest.mark.parametrize("kind", CONTRACT_KINDS)
def test_altered_commission_fails(kind: str) -> None:
    # Un punto básico de más en la comisión: precios intactos; la primera magnitud fuera
    # de tolerancia es size (el tamaño descuenta la comisión), fees o pnl.
    with pytest.raises(AssertionError, match=r"trades\.(size|fees|pnl) fila"):
        assert_engine_matches_golden(ReferenceEngine(fee_bps_delta=1.0), kind)


def test_single_fee_off_by_more_than_tolerance_fails() -> None:
    engine = MutatedEngine(_first_row("fees", pl.col("fees") * 1.001))
    with pytest.raises(AssertionError, match=r"trades\.fees fila 0"):
        assert_engine_matches_golden(engine, "sma_cross")


def test_fee_within_tolerance_passes() -> None:
    # 1e-6 relativo está dentro de RFC-0004 (1e-4): no es un defecto del adaptador.
    engine = MutatedEngine(_first_row("fees", pl.col("fees") * (1.0 + 1e-6)))
    assert_engine_matches_golden(engine, "sma_cross")


def test_exit_reason_is_compared_exactly() -> None:
    flipped = pl.lit(EXIT_REASON_END_OF_DATA, dtype=pl.Int8)
    engine = MutatedEngine(_first_row("exit_reason", flipped))
    with pytest.raises(AssertionError, match=r"trades\.exit_reason fila 0"):
        assert_engine_matches_golden(engine, "sma_cross")


def test_side_is_compared_exactly() -> None:
    # side = -1 está fuera del dominio de RF-6: la validación previa ya lo rechaza ...
    engine = MutatedEngine(_first_row("side", pl.lit(-1, dtype=pl.Int8)))
    with pytest.raises(AssertionError, match="side"):
        assert_engine_matches_golden(engine, "sma_cross")
    # ... y la comparación misma es exacta, sin tolerancia, aunque no hubiera validación.
    expected = golden_table("trades", "sma_cross")
    got = _first_row("side", pl.lit(-1, dtype=pl.Int8))(expected)
    with pytest.raises(AssertionError, match=r"trades\.side fila 0"):
        assert_tables_match("trades", got, expected)
    assert expected["exit_reason"][0] == EXIT_REASON_SIGNAL


def test_equity_drift_fails() -> None:
    class Drift(ReferenceEngine):
        def run(self, *args: Any) -> EngineOutput:
            out = super().run(*args)
            return EngineOutput(
                trades=out.trades,
                equity=out.equity.with_columns(pl.col("equity") * pl.lit(1.001, pl.Float32)),
            )

    with pytest.raises(AssertionError, match=r"equity\.equity"):
        assert_engine_matches_golden(Drift(), "donchian_breakout")


def test_invalid_output_is_reported_as_assertion() -> None:
    class Corrupt(ReferenceEngine):
        def run(self, *args: Any) -> EngineOutput:
            out = super().run(*args)
            return EngineOutput(trades=out.trades, equity=out.equity.head(10))

    with pytest.raises(AssertionError, match="output_out_of_range"):
        assert_engine_matches_golden(Corrupt(), "sma_cross")


# --------------------------------------------------------------------------- RF-21 sintética


def test_synthetic_strategy_is_not_builtin_and_has_arbitrary_signals() -> None:
    signals = core_signals(SYNTHETIC_KIND)
    entries, exits = signals.entries.to_list(), signals.exits.to_list()
    assert SYNTHETIC_KIND not in KINDS
    assert sum(entries) >= 5
    assert sum(exits) >= 5
    # warmup = 0: hay entrada en la barra 0 (se ejecuta en la apertura de la barra 1).
    assert entries[0]


def test_synthetic_strategy_exercises_the_edge_cases() -> None:
    candles = fixture_candles()
    signals = core_signals(SYNTHETIC_KIND)
    sim = simulate(
        candles,
        signals.entries.to_list(),
        signals.exits.to_list(),
        Costs(fees_bps=10.0, slippage_bps=5.0),
    )
    assert len(sim.trades) >= 5
    assert sim.trades[0].entry_time_ms == candles.open_time_ms[1]
    assert sim.trades[-1].exit_reason == EXIT_REASON_END_OF_DATA


def test_synthetic_strategy_simultaneous_signals_are_cancelled_by_the_core() -> None:
    # finalize (RF-16) anula entry+exit en la misma barra antes de llegar al motor.
    signals = core_signals(SYNTHETIC_KIND)
    assert not (signals.entries & signals.exits).any()
    t = pl.int_range(fixture_candles().n, eager=True)
    raw_both = (((t * 7) % 23) == 0) & (((t * 5 + 1) % 29) == 0)
    assert raw_both.sum() >= 1


# --------------------------------------------------------------------------- RF-15 (AST)


@pytest.mark.parametrize(
    "source",
    [
        "import finances_core.backtest.strategies\n",
        "import finances_core.backtest.catalog as c\n",
        "from finances_core.backtest import catalog\n",
        "from finances_core.backtest import engine_api, strategies\n",
        "from finances_core.backtest.strategies.base import Signals\n",
        "from finances_core.backtest.catalog import get_registry\n",
        "import importlib\nimportlib.import_module('finances_core.backtest.catalog')\n",
        "__import__('finances_core.backtest.strategies.signals')\n",
        "def f():\n    from finances_core.backtest import strategies\n",
    ],
)
def test_engine_import_of_strategies_or_catalog_is_a_violation(source: str) -> None:
    assert engine_import_violations(source, "engine.py"), source


@pytest.mark.parametrize(
    "source",
    [
        "from finances_core.backtest.engine_api import EngineOutput\n",
        "from finances_core.backtest import engine_api, frames\n",
        "import finances_core.backtest.costs\n",
        "from finances_core.schemas.backtest import CostModel\n",
        "import polars as pl\n",
    ],
)
def test_engine_allowed_imports_are_not_violations(source: str) -> None:
    assert engine_import_violations(source, "engine.py") == []


def test_isolation_scan_reports_offending_file(tmp_path: Path) -> None:
    package = tmp_path / "ft_fake_engine"
    package.mkdir()
    (package / "engine.py").write_text("import polars as pl\n", encoding="utf-8")
    (package / "service.py").write_text(
        "from finances_core.backtest.catalog import get_registry\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match=r"ft_fake_engine/service\.py:1"):
        assert_engine_modules_isolated(tmp_path)


def test_engine_packages_do_not_import_strategies_or_catalog() -> None:
    scanned = assert_engine_modules_isolated(*ENGINE_SOURCE_ROOTS)
    assert scanned >= 1
    assert all(root.is_dir() for root in ENGINE_SOURCE_ROOTS)
