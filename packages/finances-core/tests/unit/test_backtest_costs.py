"""B3.1: costes de ejecución de RF-5 (``backtest/costs.py``) con ejemplos calculados a mano."""

from __future__ import annotations

import polars as pl
import pytest

from finances_core.backtest.costs import buy_price, fee_rate, sell_price, slip
from finances_core.schemas.backtest import CostModel

COSTS = CostModel(fees_bps=10, slippage_bps=5)
#: Valores por defecto de A7: ``fees_bps = 10``, ``slippage_bps = 0``.
DEFAULT_COSTS = CostModel(fees_bps=10, slippage_bps=0)


def test_fee_rate_is_bps_over_ten_thousand() -> None:
    assert fee_rate(COSTS) == pytest.approx(0.001)
    assert fee_rate(CostModel(fees_bps=25, slippage_bps=0)) == pytest.approx(0.0025)
    assert fee_rate(CostModel(fees_bps=0, slippage_bps=0)) == 0.0


def test_slip_is_bps_over_ten_thousand() -> None:
    assert slip(COSTS) == pytest.approx(0.0005)
    assert slip(DEFAULT_COSTS) == 0.0


def test_buy_price_adds_slippage_to_open() -> None:
    # 100 · (1 + 0,0005) = 100,05
    assert buy_price(100.0, COSTS) == pytest.approx(100.05)
    assert buy_price(100.0, DEFAULT_COSTS) == 100.0


def test_sell_price_subtracts_slippage_from_open() -> None:
    # 100 · (1 - 0,0005) = 99,95
    assert sell_price(100.0, COSTS) == pytest.approx(99.95)
    assert sell_price(100.0, DEFAULT_COSTS) == 100.0


def test_round_trip_with_default_costs_matches_hand_example() -> None:
    """RF-5 reglas 3-5: caja 10 000, ``size_fraction = 1``, apertura 100, comisión 10 bps."""
    cash = 10_000.0
    price = buy_price(100.0, DEFAULT_COSTS)
    size = 1.0 * cash / (price * (1 + fee_rate(DEFAULT_COSTS)))
    entry_fee = fee_rate(DEFAULT_COSTS) * price * size
    assert size == pytest.approx(99.9000999000999)
    assert entry_fee == pytest.approx(9.990009990009991)
    # La caja cubre la comisión: queda en 0 tras entrar.
    assert cash - price * size - entry_fee == pytest.approx(0.0, abs=1e-9)


def test_prices_accept_polars_expressions() -> None:
    frame = pl.DataFrame({"open": pl.Series([100.0, 200.0], dtype=pl.Float32)})
    out = frame.select(
        buy=buy_price(pl.col("open"), COSTS),
        sell=sell_price(pl.col("open"), COSTS),
    )
    assert out.schema == pl.Schema({"buy": pl.Float32, "sell": pl.Float32})
    assert out["buy"].to_list() == pytest.approx([100.05, 200.1], rel=1e-6)
    assert out["sell"].to_list() == pytest.approx([99.95, 199.9], rel=1e-6)
