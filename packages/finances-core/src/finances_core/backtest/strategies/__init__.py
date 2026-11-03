"""Contrato de estrategias y estrategias integradas (RF-16…RF-20).

Cada subcarpeta cuyo nombre no empieza por ``_`` ni ``.`` es una estrategia integrada
(``<kind>/logic.py`` + ``README.md``). Las estrategias, integradas o locales, importan su
contrato desde aquí: ``from finances_core.backtest.strategies import BaseStrategy, Signals``.
"""

from finances_core.backtest.strategies.base import (
    BaseStrategy,
    MetricsTarget,
    Signals,
    StrategyDefinitionError,
    validate_strategy_class,
)

__all__ = [
    "BaseStrategy",
    "MetricsTarget",
    "Signals",
    "StrategyDefinitionError",
    "validate_strategy_class",
]
