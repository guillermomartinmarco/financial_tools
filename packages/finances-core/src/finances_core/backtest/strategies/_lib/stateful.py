"""Indicadores con estado de ``_lib`` (Enmienda L2d): recursión condicional no expresable
como ``pl.Expr``.

``supertrend`` y ``sar`` tienen una recursión que depende de ramas (reversión de tendencia),
así que se calculan con un **scan causal puro**: un bucle Python sobre la columna en doble
precisión escalar (floats nativos de Python, **sin NumPy**, como el bootstrap de G5), que
recibe ``pl.Series`` y devuelve ``pl.Series`` ``Float32``. Son deterministas y solo miran
barras ``<= t`` (RF-20): truncar la serie en ``t`` no cambia el valor en ``t`` (causalidad,
verificada en los tests). La excepción RFC-0004 (NumPy/pandas) sigue siendo exclusiva de
``ft_<motor>/engine.py``; acá no hay NumPy ni columnas de doble precisión, solo escalares.

Ambos reproducen la convención de TA-Lib (oráculo), verificada en la cross-validación viva.
"""

from __future__ import annotations

import polars as pl

#: SAR necesita al menos 2 barras (sacrifica la primera para la semilla; ``ta_SAR.c``).
_SAR_MIN_BARS = 2


def supertrend(
    high: pl.Series, low: pl.Series, close: pl.Series, period: int, multiplier: float
) -> tuple[pl.Series, pl.Series]:
    """SuperTrend (convención TA-Lib). Devuelve ``(supertrend Float32, trend Int8)``.

    ``ATR`` de Wilder (periodo ``period``, semilla SMA de ``TR[1..period]``), bandas básicas
    ``hl2 ± multiplier·ATR`` con ``hl2 = (high+low)/2``; las bandas finales "pegan" según el
    cierre previo. ``supertrend`` es una magnitud (``Float32``); ``trend`` es una **columna
    categórica** de dominio ``{-1, +1}`` → ``Int8`` (Art. 5.1 bis, como ``position_from_events``).
    Nulo (ambas salidas) para ``t < period``; el primer valor cae en ``t = period``.
    """
    highs = high.to_list()
    lows = low.to_list()
    closes = close.to_list()
    n = len(closes)
    st: list[float | None] = [None] * n
    trend: list[int | None] = [None] * n
    if n <= period:
        return (
            pl.Series("supertrend", st, dtype=pl.Float32),
            pl.Series("trend", trend, dtype=pl.Int8),
        )

    # True range y ATR de Wilder (escalares Python); TR[0] = high-low (sin close previo).
    tr = [highs[0] - lows[0]] + [
        max(highs[t] - lows[t], abs(highs[t] - closes[t - 1]), abs(lows[t] - closes[t - 1]))
        for t in range(1, n)
    ]
    atr = sum(tr[1 : period + 1]) / period  # semilla en t = period

    prev_fu = prev_fl = prev_st = 0.0
    for t in range(period, n):
        if t > period:
            atr = (atr * (period - 1) + tr[t]) / period
        hl2 = (highs[t] + lows[t]) / 2.0
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        if t == period:
            final_upper, final_lower = basic_upper, basic_lower
            direction = 1 if closes[t] > final_lower else -1
            current = final_lower if direction == 1 else final_upper
        else:
            final_upper = (
                basic_upper if (basic_upper < prev_fu or closes[t - 1] > prev_fu) else prev_fu
            )
            final_lower = (
                basic_lower if (basic_lower > prev_fl or closes[t - 1] < prev_fl) else prev_fl
            )
            if prev_st == prev_fu:  # venía en tendencia bajista (stop = banda superior)
                direction = -1 if closes[t] <= final_upper else 1
                current = final_upper if direction == -1 else final_lower
            else:  # venía en tendencia alcista (stop = banda inferior)
                direction = 1 if closes[t] >= final_lower else -1
                current = final_lower if direction == 1 else final_upper
        st[t] = current
        trend[t] = direction
        prev_fu, prev_fl, prev_st = final_upper, final_lower, current

    return (
        pl.Series("supertrend", st, dtype=pl.Float32),
        pl.Series("trend", trend, dtype=pl.Int8),
    )


def sar(  # noqa: PLR0915 (port 1:1 de ta_SAR.c; monolítico a propósito para fidelidad)
    high: pl.Series, low: pl.Series, acceleration: float, maximum: float
) -> pl.Series:
    """Parabolic SAR de Wilder (convención TA-Lib). Devuelve ``pl.Series`` ``Float32``.

    Port fiel de ``ta_SAR.c``: la dirección inicial sale del ``-DM`` entre las dos primeras
    barras (empate → largo); TA-Lib "consume" la primera barra y usa su ``high/low`` como SAR
    inicial de la segunda. ``af`` arranca en ``acceleration`` y sube ``acceleration`` por cada
    nuevo extremo hasta ``maximum``; en cada barra el SAR se recorta al rango de la barra
    actual y la previa. Nulo en ``t = 0``; el primer valor cae en ``t = 1``.
    """
    highs = high.to_list()
    lows = low.to_list()
    n = len(highs)
    out: list[float | None] = [None] * n
    if n < _SAR_MIN_BARS:
        return pl.Series("sar", out, dtype=pl.Float32)

    accel = acceleration
    af = accel if accel <= maximum else maximum
    accel = af
    # Dirección inicial vía -DM de la barra 0->1 (empate o +DM mayor -> largo).
    diff_p = highs[1] - highs[0]
    diff_m = lows[0] - lows[1]
    minus_dm = diff_m if (diff_m > 0 and diff_m > diff_p) else 0.0
    is_long = minus_dm <= 0

    today = 1
    if is_long:
        ep = highs[1]
        current = lows[0]
    else:
        ep = lows[1]
        current = highs[0]
    new_low = lows[1]
    new_high = highs[1]
    out_idx = 1
    while today <= n - 1:
        prev_low, prev_high = new_low, new_high
        new_low, new_high = lows[today], highs[today]
        today += 1
        if is_long:
            if new_low <= current:  # reversión a corto
                is_long = False
                current = max(ep, prev_high, new_high)
                out[out_idx] = current
                af = accel
                ep = new_low
                current = current + af * (ep - current)
                current = max(current, prev_high, new_high)
            else:
                out[out_idx] = current
                if new_high > ep:
                    ep = new_high
                    af = min(af + accel, maximum)
                current = current + af * (ep - current)
                current = min(current, prev_low, new_low)
        elif new_high >= current:  # reversión a largo
            is_long = True
            current = min(ep, prev_low, new_low)
            out[out_idx] = current
            af = accel
            ep = new_high
            current = current + af * (ep - current)
            current = min(current, prev_low, new_low)
        else:
            out[out_idx] = current
            if new_low < ep:
                ep = new_low
                af = min(af + accel, maximum)
            current = current + af * (ep - current)
            current = max(current, prev_high, new_high)
        out_idx += 1

    return pl.Series("sar", out, dtype=pl.Float32)
