"""Evaluación en memoria de un backtest para parámetros arbitrarios de un ``kind``.

Pensada para optimizadores (p. ej. ``ft_vectorbt optimize``): calcula las señales del núcleo
para ``(kind, params)`` y simula con el motor **en memoria**, sin resolver una estrategia
registrada (``strategy_source.load`` usa los ``params`` de la base) y **sin persistir**. Vive
en el núcleo porque necesita la clase de la estrategia del registro (``catalog``), inaccesible
desde las herramientas motor por aislamiento (RF-21); estas la consumen vía esta función.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import polars as pl

from finances_core.backtest.catalog import StrategyRegistry, get_registry
from finances_core.backtest.engine_api import BacktestEngine, CostModel, EngineOutput
from finances_core.backtest.errors import StrategyRuntimeError
from finances_core.backtest.frames import validate_engine_output
from finances_core.backtest.strategies.signals import finalize
from finances_core.config import FinancesSettings


def evaluate_params(  # noqa: PLR0913 - modelo de ejecución completo (RF-5) con kw-only
    *,
    kind: str,
    params: Mapping[str, Any],
    candles: pl.DataFrame,
    engine: BacktestEngine,
    costs: CostModel,
    size_fraction: float,
    initial_cash: float,
    settings: FinancesSettings | None = None,
    registry: StrategyRegistry | None = None,
) -> EngineOutput:
    """Calcula señales de ``(kind, params)`` y simula con ``engine`` (en memoria, sin persistir).

    Args:
        kind: estrategia registrada (integrada o local).
        params: parámetros a probar (se validan contra ``Strategy.Params``).
        candles: velas canónicas (``frame`` de ``read_candles``) de la ventana a evaluar.
        engine: motor que solo simula (RF-21).
        costs / size_fraction / initial_cash: modelo de ejecución (RF-5).
        settings: config; por defecto la del entorno.
        registry: registro de estrategias; por defecto el del proceso.

    Raises:
        UnknownKindError: SI ``kind`` no está registrado.
        StrategyInitError: SI ``kind`` es una carpeta local inválida.
        StrategyRuntimeError: SI ``warmup``/``signals`` lanzan una excepción.
        InvalidSignalsError: SI las señales no son canónicas.
        OutputOutOfRangeError: SI la salida del motor no es canónica.
    """
    active = registry if registry is not None else get_registry(settings or FinancesSettings())
    strategy_cls = active.get(kind)
    validated = strategy_cls.Params.model_validate(dict(params))
    try:
        warmup = int(strategy_cls.warmup(validated))
        raw = strategy_cls.signals(candles, validated)
    except Exception as exc:
        raise StrategyRuntimeError(kind, exc) from exc
    signals = finalize(raw, n=candles.height, warmup=warmup)
    output = engine.run(candles, signals, costs, size_fraction, initial_cash)
    validate_engine_output(output, candles.height)
    return output
