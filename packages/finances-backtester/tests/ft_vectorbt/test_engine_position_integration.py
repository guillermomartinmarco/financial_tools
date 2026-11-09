"""G2b: ``position_to_signals`` (núcleo) → ``VectorbtEngine`` real, de punta a punta.

Las demás suites prueban por separado la derivación de flancos (``finances-core``) y la
economía del motor con ``Signals`` construidas a mano (``test_engine_short_reference.py``).
Esta prueba cierra el eslabón: una posición ``pos`` real (como la que devolvería una
estrategia) atraviesa ``position_to_signals`` y llega al motor VectorBT real, con largo,
corto y plano en la misma corrida.
"""

from __future__ import annotations

import polars as pl
import pytest
from finances_core.backtest.errors import InvalidSignalsError
from finances_core.backtest.strategies.signals import position_to_signals

from tests.ft_vectorbt.conftest import NO_COSTS, make_candles, run_pipeline


def test_position_with_long_flat_and_short_runs_through_the_real_engine() -> None:
    # pos: largo t0-2, plano t3, corto t4-6, plano t7 (sin reversa directa).
    pos = pl.Series([1, 1, 1, 0, -1, -1, -1, 0], dtype=pl.Int8)
    signals = position_to_signals(pos)
    candles = make_candles(
        open_=[100.0, 105.0, 110.0, 108.0, 108.0, 100.0, 95.0, 95.0],
        close=[100.0, 105.0, 110.0, 108.0, 108.0, 100.0, 95.0, 95.0],
    )
    output = run_pipeline(candles, signals, costs=NO_COSTS, size_fraction=1.0, initial_cash=1000.0)

    sides = output.trades["side"].to_list()
    assert sides == [1, -1]  # un trade largo y uno corto, en ese orden
    long_trade, short_trade = output.trades.to_dicts()
    assert long_trade["pnl"] > 0  # largo: compró a 105 (t1 shift), vendió a 108 (t4 shift)
    assert short_trade["pnl"] > 0  # corto: vendió a 100 (t5 shift), compró a 95 (t7 shift)
    assert output.equity.height == candles.height


def test_short_rejects_direct_reversal_before_reaching_the_engine() -> None:
    pos = pl.Series([1, 1, -1, -1], dtype=pl.Int8)  # sin 0 intermedio
    with pytest.raises(InvalidSignalsError, match="invalid_signals"):
        position_to_signals(pos)
