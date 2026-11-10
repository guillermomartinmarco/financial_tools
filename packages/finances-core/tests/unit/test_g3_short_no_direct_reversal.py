"""G3: ninguna variante corta de una estrategia integrada produce reversa directa.

Prueba de cierre del esfuerzo G3 (Enmiendas G2b/G3): sobre la serie sint\u00e9tica de
contrato (600 barras), ninguna de las 4 estrategias de muestra (una por patr\u00f3n:
direccional degenerado, umbral sim\u00e9trico negado, reflejo sim\u00e9trico simple, combinador
invertido) produce una transici\u00f3n directa ``+1`` <-> ``-1`` entre barras consecutivas, y
``position_to_signals`` nunca rechaza la posici\u00f3n resultante.
"""

from __future__ import annotations

import pytest

from finances_core.backtest.catalog import get_registry
from finances_core.backtest.strategies.signals import finalize, position_to_signals
from finances_core.backtest.strategies.testing import synthetic_candles
from finances_core.config import FinancesSettings

_PARAMS = {
    "donchian_breakout": {"window": 5, "allow_short": True},
    "momentum_roc": {"n": 3, "threshold": 0.0, "allow_short": True},
    "zscore_reversion": {"n": 5, "threshold": 1.0, "allow_short": True},
    "trend_filtered_reversion": {
        "trend_n": 50,
        "rsi_period": 2,
        "rsi_lower": 30.0,
        "rsi_upper": 60.0,
        "allow_short": True,
    },
}


@pytest.mark.parametrize("kind", sorted(_PARAMS))
def test_short_variant_has_no_direct_reversal(kind: str) -> None:
    strategy = get_registry(FinancesSettings()).get(kind)
    params = strategy.Params(**_PARAMS[kind])
    candles = synthetic_candles()
    pos = finalize(
        strategy.signals(candles, params), n=candles.height, warmup=strategy.warmup(params)
    )
    values = pos.to_list()
    assert any(v == 1 for v in values), f"{kind}: la muestra no abre ning\u00fan largo"
    assert any(v == -1 for v in values), f"{kind}: la muestra no abre ning\u00fan corto"
    for prev, cur in zip(values, values[1:], strict=False):
        assert not (prev == 1 and cur == -1), f"{kind}: reversa directa +1->-1"
        assert not (prev == -1 and cur == 1), f"{kind}: reversa directa -1->+1"
    position_to_signals(pos)  # no debe lanzar invalid_signals
