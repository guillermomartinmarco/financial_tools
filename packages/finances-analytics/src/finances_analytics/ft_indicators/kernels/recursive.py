"""Kernels recursivos: ``ema``, ``rsi``, ``macd``, ``atr``, ``adx`` (RF-2, plan §3.3).

Todos usan :func:`~.common.ewm_seeded` (semilla SMA + ``ewm_mean(adjust=False,
ignore_nulls=True)``) sobre la subsecuencia de filas válidas: la fila nula recibe salida
nula y se excluye de la recursión (RF-3). Suavizado de Wilder = ``α = 1/period``; las sumas
de Wilder de ADX (``S_t = S_{t−1} − S_{t−1}/n + x_t``) son la media de Wilder × ``period``
y ``period`` se cancela en los cocientes ±DI.
"""

from __future__ import annotations

import polars as pl

from .common import (
    COMPUTE_DTYPE,
    Outputs,
    Params,
    Program,
    all_valid,
    ewm_seeded,
    int_param,
    previous_valid,
    source_column,
    wilder_sum_seeded,
)


def _ema_of(program: Program, value: pl.Expr, period: int) -> pl.Expr:
    return ewm_seeded(program, value, period, 2.0 / (period + 1))


def _wilder_of(program: Program, value: pl.Expr, period: int) -> pl.Expr:
    return ewm_seeded(program, value, period, 1.0 / period)


def ema(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """EMA clásica: α = 2/(period+1), semilla = SMA de las ``period`` primeras filas."""
    value = program.float_input(source_column(params), dtype)
    return {"": _ema_of(program, value, int_param(params, "period"))}


def rsi(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """RSI de Wilder (1978); **SI** G + L = 0 (sin movimiento) **ENTONCES** 0 (TA-Lib)."""
    period = int_param(params, "period")
    value = program.float_input(source_column(params), dtype)
    # Cambio respecto de la fila válida anterior: nulo en filas nulas y en la primera válida.
    change = program.let(value - previous_valid(program, value))
    gain = _wilder_of(program, change.clip(lower_bound=0.0), period)
    loss = _wilder_of(program, (-change).clip(lower_bound=0.0), period)
    total = gain + loss
    return {"": pl.when(total == 0).then(0.0).otherwise(100.0 * (gain / total))}


def macd(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """MACD de Appel (StockCharts): cada EMA con su propia semilla SMA.

    line = EMA(fast) − EMA(slow); signal = EMA(``signal``) de line sembrada con la SMA de
    sus ``signal`` primeros valores; hist = line − signal.
    """
    value = program.float_input(source_column(params), dtype)
    fast = _ema_of(program, value, int_param(params, "fast"))
    slow = _ema_of(program, value, int_param(params, "slow"))
    line = program.let(fast - slow)
    signal = _ema_of(program, line, int_param(params, "signal"))
    return {"line": line, "signal": signal, "hist": line - signal}


def _hlc(program: Program, dtype: type[pl.DataType]) -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    """``high``, ``low``, ``close`` anulados en conjunto si alguno es inválido."""
    high, low, close = (program.float_input(c, dtype) for c in ("high", "low", "close"))
    valid = program.let(all_valid(high, low, close))
    return (
        program.let(pl.when(valid).then(high)),
        program.let(pl.when(valid).then(low)),
        program.let(pl.when(valid).then(close)),
    )


def true_range(program: Program, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> pl.Expr:
    """TR_t = max(H−L, |H−C_{t−1}|, |L−C_{t−1}|) desde la segunda fila válida.

    ``C_{t−1}`` es el cierre de la fila válida anterior.
    """
    high, low, close = _hlc(program, dtype)
    prev_close = previous_valid(program, close)
    ranges = pl.max_horizontal(high - low, (high - prev_close).abs(), (low - prev_close).abs())
    return program.let(pl.when(close.is_not_null() & prev_close.is_not_null()).then(ranges))


def atr(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """ATR de Wilder: semilla = media de TR[1..period] en ``t = period`` (como TA-Lib)."""
    return {"": _wilder_of(program, true_range(program, dtype), int_param(params, "period"))}


def adx(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """ADX/±DI de Wilder.

    +DM = H_t − H_{t−1} si supera a L_{t−1} − L_t y es > 0 (si no 0); −DM simétrico. Sumas
    de Wilder con la semilla de TA-Lib: Σ de los ``period−1`` primeros valores + un paso de
    Wilder en ``t = period`` (:func:`~.common.wilder_sum_seeded`);
    ±DI = 100·S±/S_TR (S_TR = 0 → 0); DX = 100·|+DI − −DI|/(+DI + −DI) (suma 0 → 0);
    ADX = Wilder de DX sembrado con la media de los ``period`` primeros DX (``t = 2·period−1``).
    """
    period = int_param(params, "period")
    high, low, _close = _hlc(program, dtype)
    up = program.let(high - previous_valid(program, high))
    down = program.let(previous_valid(program, low) - low)
    plus_dm = pl.when(up.is_null()).then(None).when((up > down) & (up > 0)).then(up).otherwise(0.0)
    minus_dm = (
        pl.when(down.is_null()).then(None).when((down > up) & (down > 0)).then(down).otherwise(0.0)
    )
    smoothed_tr = wilder_sum_seeded(program, true_range(program, dtype), period)
    smoothed_plus = wilder_sum_seeded(program, plus_dm, period)
    smoothed_minus = wilder_sum_seeded(program, minus_dm, period)
    plus_di = program.let(
        pl.when(smoothed_tr == 0).then(0.0).otherwise(100.0 * smoothed_plus / smoothed_tr)
    )
    minus_di = program.let(
        pl.when(smoothed_tr == 0).then(0.0).otherwise(100.0 * smoothed_minus / smoothed_tr)
    )
    di_sum = plus_di + minus_di
    dx = pl.when(di_sum == 0).then(0.0).otherwise(100.0 * (plus_di - minus_di).abs() / di_sum)
    return {"": _wilder_of(program, dx, period), "plus_di": plus_di, "minus_di": minus_di}


__all__ = ["adx", "atr", "ema", "macd", "rsi", "true_range"]
