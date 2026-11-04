"""Costes de ejecución de RF-5 (reglas 3 y 4), idénticos para todos los motores.

``fee_rate = fees_bps / 10 000`` y ``s = slippage_bps / 10 000``; la compra se ejecuta a
``open·(1 + s)`` y la venta a ``open·(1 - s)``. Los precios aceptan un escalar o una
expresión Polars (``pl.col("open")``), que conserva el dtype ``Float32`` de la columna.
"""

from __future__ import annotations

from typing import overload

import polars as pl

from finances_core.schemas.backtest import CostModel

#: Puntos básicos por unidad.
BPS = 10_000


def fee_rate(costs: CostModel) -> float:
    """Tasa de comisión por ejecución: ``fees_bps / 10 000``."""
    return costs.fees_bps / BPS


def slip(costs: CostModel) -> float:
    """Deslizamiento relativo ``s = slippage_bps / 10 000``."""
    return costs.slippage_bps / BPS


@overload
def buy_price(open_price: float, costs: CostModel) -> float: ...
@overload
def buy_price(open_price: pl.Expr, costs: CostModel) -> pl.Expr: ...
def buy_price(open_price: float | pl.Expr, costs: CostModel) -> float | pl.Expr:
    """Precio de compra con deslizamiento: ``open·(1 + s)``."""
    return open_price * (1 + slip(costs))


@overload
def sell_price(open_price: float, costs: CostModel) -> float: ...
@overload
def sell_price(open_price: pl.Expr, costs: CostModel) -> pl.Expr: ...
def sell_price(open_price: float | pl.Expr, costs: CostModel) -> float | pl.Expr:
    """Precio de venta con deslizamiento: ``open·(1 - s)``."""
    return open_price * (1 - slip(costs))
