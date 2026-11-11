"""Patrones de velas de ``_lib`` (Enmienda L2e): booleanos ``pl.Expr`` (``True`` = patrón en ``t``).

Subconjunto curado (no los ~61 de TA-Lib). Cada patrón se define con **reglas propias,
autocontenidas y documentadas** sobre la(s) vela(s) implicada(s) —cuerpo, rango y sombras de la
barra, sin promedios móviles— y se valida contra los ``CDL*`` de TA-Lib (oráculo) sobre casos
de libro (los ``CDL*`` usan umbrales adaptativos por promedios, así que el acuerdo exacto sobre
ruido aleatorio no es un objetivo; sí lo es coincidir en patrones inequívocos).

Todos devuelven ``pl.Boolean`` (``False``, nunca nulo, en el calentamiento: RF-2) y solo miran
barras ``<= t`` (RF-20): los multibarra componen ``.shift(k)`` del pasado.
"""

from __future__ import annotations

import polars as pl

#: Cuerpo "doji": a lo sumo este fracción del rango de la barra.
_DOJI_BODY = 0.1
#: Cuerpo "largo" / "chico" como fracción del rango de su barra.
_LONG_BODY = 0.6
_SMALL_BODY = 0.3
#: Sombra "larga" relativa al cuerpo (martillo / estrella fugaz).
_SHADOW_RATIO = 2.0
#: Sombra "casi nula" de un marubozu, como fracción del rango.
_MARU_SHADOW = 0.05


def _body(open_: pl.Expr, close: pl.Expr) -> pl.Expr:
    return (close - open_).abs()


def _upper_shadow(open_: pl.Expr, high: pl.Expr, close: pl.Expr) -> pl.Expr:
    return high - pl.max_horizontal(open_, close)


def _lower_shadow(open_: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    return pl.min_horizontal(open_, close) - low


def _b(expr: pl.Expr) -> pl.Expr:
    """Castea a ``Boolean`` y trata el calentamiento (nulos por ``shift``) como ``False``."""
    return expr.fill_null(value=False).cast(pl.Boolean)


def doji(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Doji: cuerpo despreciable frente al rango (``|close-open| <= 0.1·(high-low)``)."""
    rng = high - low
    return _b((rng > 0) & (_body(open_, close) <= _DOJI_BODY * rng))


def hammer(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Martillo: sombra inferior larga (``>= 2·cuerpo``), sombra superior chica, cuerpo arriba."""
    body = _body(open_, close)
    return _b(
        (body > 0)
        & (_lower_shadow(open_, low, close) >= _SHADOW_RATIO * body)
        & (_upper_shadow(open_, high, close) <= body)
    )


def shooting_star(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Estrella fugaz: sombra superior larga (``>= 2·cuerpo``), sombra inferior chica."""
    body = _body(open_, close)
    return _b(
        (body > 0)
        & (_upper_shadow(open_, high, close) >= _SHADOW_RATIO * body)
        & (_lower_shadow(open_, low, close) <= body)
    )


def marubozu(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Marubozu: cuerpo largo sin sombras (ambas ``<= 0.05·rango``)."""
    rng = high - low
    return _b(
        (rng > 0)
        & (_body(open_, close) >= _LONG_BODY * rng)
        & (_upper_shadow(open_, high, close) <= _MARU_SHADOW * rng)
        & (_lower_shadow(open_, low, close) <= _MARU_SHADOW * rng)
    )


def bullish_engulfing(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Envolvente alcista: vela previa bajista cuyo cuerpo queda envuelto por una alcista."""
    prev_open, prev_close = open_.shift(1), close.shift(1)
    return _b(
        (prev_close < prev_open) & (close > open_) & (open_ <= prev_close) & (close >= prev_open)
    )


def bearish_engulfing(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Envolvente bajista: vela previa alcista cuyo cuerpo queda envuelto por una bajista."""
    prev_open, prev_close = open_.shift(1), close.shift(1)
    return _b(
        (prev_close > prev_open) & (close < open_) & (open_ >= prev_close) & (close <= prev_open)
    )


def harami(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Harami: cuerpo chico contenido dentro del cuerpo largo previo (cualquier signo)."""
    prev_open, prev_close = open_.shift(1), close.shift(1)
    prev_body = (prev_close - prev_open).abs()
    prev_hi = pl.max_horizontal(prev_open, prev_close)
    prev_lo = pl.min_horizontal(prev_open, prev_close)
    cur_hi = pl.max_horizontal(open_, close)
    cur_lo = pl.min_horizontal(open_, close)
    rng = high - low
    return _b(
        (prev_body > 0)
        & (_body(open_, close) <= _SMALL_BODY * prev_body)
        & (cur_hi <= prev_hi)
        & (cur_lo >= prev_lo)
        & (rng >= 0)
    )


def piercing(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Piercing: tras una bajista, abre bajo su mínimo y cierra sobre el medio de su cuerpo."""
    prev_open, prev_close = open_.shift(1), close.shift(1)
    prev_mid = (prev_open + prev_close) / 2.0
    return _b(
        (prev_close < prev_open)
        & (close > open_)
        & (open_ < low.shift(1))
        & (close > prev_mid)
        & (close < prev_open)
    )


def dark_cloud_cover(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Dark cloud cover: tras una alcista, abre sobre su máximo y cierra bajo su medio."""
    prev_open, prev_close = open_.shift(1), close.shift(1)
    prev_mid = (prev_open + prev_close) / 2.0
    return _b(
        (prev_close > prev_open)
        & (close < open_)
        & (open_ > high.shift(1))
        & (close < prev_mid)
        & (close > prev_open)
    )


def morning_star(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Estrella de la mañana (3 velas): bajista larga, cuerpo chico que hace gap, alcista larga."""
    o1, c1, h1, l1 = open_.shift(2), close.shift(2), high.shift(2), low.shift(2)
    o2, c2 = open_.shift(1), close.shift(1)
    body1 = (c1 - o1).abs()
    body2 = (c2 - o2).abs()
    body3 = _body(open_, close)
    mid1 = (o1 + c1) / 2.0
    return _b(
        (c1 < o1)
        & (body1 >= _LONG_BODY * (h1 - l1))
        & (body2 <= _SMALL_BODY * body1)
        & (pl.max_horizontal(o2, c2) < c1)
        & (close > open_)
        & (body3 >= _LONG_BODY * (high - low))
        & (close > mid1)
    )


def evening_star(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """Estrella de la tarde (3 velas): alcista larga, cuerpo chico que hace gap, bajista larga."""
    o1, c1, h1, l1 = open_.shift(2), close.shift(2), high.shift(2), low.shift(2)
    o2, c2 = open_.shift(1), close.shift(1)
    body1 = (c1 - o1).abs()
    body2 = (c2 - o2).abs()
    body3 = _body(open_, close)
    mid1 = (o1 + c1) / 2.0
    return _b(
        (c1 > o1)
        & (body1 >= _LONG_BODY * (h1 - l1))
        & (body2 <= _SMALL_BODY * body1)
        & (pl.min_horizontal(o2, c2) > c1)
        & (close < open_)
        & (body3 >= _LONG_BODY * (high - low))
        & (close < mid1)
    )
