"""Kernels de memoria finita: ``sma``, ``wma``, ``bb``, ``roc``, ``vwap``, ``stoch`` (RF-2).

Ventana completa obligatoria: **SI** la ventana contiene una fila nula (o no finita),
**ENTONCES** la salida es nula (RF-3). Las sumas de ventana se hacen sobre la entrada con
los nulos rellenos a 0 (misma ruta de cálculo con o sin nulos: prefijo estable bit a bit)
y la máscara de nulos se aplica después.
"""

from __future__ import annotations

import polars as pl

from .common import (
    COMPUTE_DTYPE,
    Outputs,
    Params,
    Program,
    all_valid,
    float_param,
    int_param,
    null_count_in_window,
    scaled,
    source_column,
)


def _window_sum(value: pl.Expr, window: int, weights: list[float] | None = None) -> pl.Expr:
    return value.fill_null(0.0).rolling_sum(window, weights=weights)


def _full_window(valid: pl.Expr, window: int) -> pl.Expr:
    """``True`` si la ventana de ``window`` filas está completa y sin filas inválidas."""
    return null_count_in_window(valid, window) == 0


def _sma_of(value: pl.Expr, period: int) -> pl.Expr:
    return pl.when(_full_window(value.is_not_null(), period)).then(
        scaled(_window_sum(value, period), period)
    )


def sma(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """Media aritmética de las últimas ``period`` filas (calentamiento ``period−1``)."""
    value = program.float_input(source_column(params), dtype)
    return {"": _sma_of(value, int_param(params, "period"))}


def wma(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """Media ponderada lineal: pesos 1..``period``, el más reciente pesa ``period``."""
    period = int_param(params, "period")
    value = program.float_input(source_column(params), dtype)
    weights = [float(w) for w in range(1, period + 1)]  # weights[0] = fila más antigua
    denominator = period * (period + 1) / 2
    weighted = scaled(_window_sum(value, period, weights), denominator)
    return {"": pl.when(_full_window(value.is_not_null(), period)).then(weighted)}


def bb(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """Bandas de Bollinger: SMA ± ``std``·σ poblacional (ddof = 0, como Bollinger y TA-Lib)."""
    period = int_param(params, "period")
    width = float_param(params, "std")
    value = program.float_input(source_column(params), dtype)
    full = program.let(_full_window(value.is_not_null(), period))
    middle = program.let(scaled(_window_sum(value, period), period))
    sigma = program.let(value.fill_null(0.0).rolling_std(period, ddof=0))
    return {
        "middle": pl.when(full).then(middle),
        "upper": pl.when(full).then(middle + width * sigma),
        "lower": pl.when(full).then(middle - width * sigma),
    }


def roc(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """``100·(x_t / x_{t−period} − 1)``; **SI** la base es 0 **ENTONCES** nulo.

    La ventana es ``[t−period, t]`` (``period+1`` filas): una fila nula en ella anula la salida.
    """
    period = int_param(params, "period")
    value = program.float_input(source_column(params), dtype)
    base = value.shift(period)
    full = _full_window(value.is_not_null(), period + 1)
    return {"": pl.when(full & (base != 0)).then((value / base - 1.0) * 100.0)}


def vwap(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """VWAP móvil: ``Σ(TP·V)/ΣV`` en ``period`` filas, ``TP = (H+L+C)/3``; ``ΣV = 0`` → nulo.

    ``ΣV = 0`` se detecta contando filas con volumen distinto de 0 (exacto, sin depender del
    redondeo de la suma móvil).
    """
    period = int_param(params, "period")
    high, low, close, volume = (
        program.float_input(c, dtype) for c in ("high", "low", "close", "volume")
    )
    valid = program.let(all_valid(high, low, close, volume))
    price_volume = pl.when(valid).then(scaled(high + low + close, 3) * volume)
    volume_sum = _window_sum(pl.when(valid).then(volume), period)
    nonzero = (volume != 0).fill_null(value=False).cast(pl.UInt32).rolling_sum(period)
    return {
        "": pl.when(_full_window(valid, period) & (nonzero > 0)).then(
            _window_sum(price_volume, period) / volume_sum
        )
    }


def stoch_fast_k(program: Program, k: int, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> pl.Expr:
    """%K rápido ``100·(C − LL_k)/(HH_k − LL_k)``; **SI** HH = LL **ENTONCES** 0 (TA-Lib).

    Nulo si la ventana de ``k`` filas tiene alguna fila inválida.
    """
    high, low, close = (program.float_input(c, dtype) for c in ("high", "low", "close"))
    valid = program.let(all_valid(high, low, close))
    highest = program.let(pl.when(valid).then(high).rolling_max(k))
    lowest = program.let(pl.when(valid).then(low).rolling_min(k))
    ratio = (close - lowest) / (highest - lowest) * 100.0
    fast_k = (
        pl.when(~_full_window(valid, k))
        .then(None)
        .when(highest == lowest)
        .then(0.0)
        .otherwise(ratio)
    )
    return program.let(fast_k)


def stoch(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """Estocástico completo (*Full Stochastic* de StockCharts, STOCH de TA-Lib con SMA).

    %K = SMA(%K rápido, ``smooth_k``); %D = SMA(%K, ``d``).
    """
    fast_k = stoch_fast_k(program, int_param(params, "k"), dtype)
    slow_k = program.let(_sma_of(fast_k, int_param(params, "smooth_k")))
    return {"k": slow_k, "d": _sma_of(slow_k, int_param(params, "d"))}


__all__ = ["bb", "roc", "sma", "stoch", "stoch_fast_k", "vwap", "wma"]
