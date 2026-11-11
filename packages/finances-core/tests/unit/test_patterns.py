"""L2e: goldens hand-computed de los patrones de velas de ``_lib`` (sin ``talib``, RF-2).

Cada test arma velas inequívocas y verifica el booleano en la barra señal (y ``False`` donde no
hay patrón o falta contexto). La aritmética de cuerpo/rango/sombras va en los comentarios. La
concordancia con los ``CDL*`` de TA-Lib se valida aparte (cross-val viva en finances-analytics).
"""

from __future__ import annotations

import polars as pl

from finances_core.backtest.strategies._lib.patterns import (
    bearish_engulfing,
    bullish_engulfing,
    dark_cloud_cover,
    doji,
    evening_star,
    hammer,
    harami,
    marubozu,
    morning_star,
    piercing,
    shooting_star,
)


def _ohlc(
    open_: list[float], high: list[float], low: list[float], close: list[float]
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open": pl.Series(open_, dtype=pl.Float32),
            "high": pl.Series(high, dtype=pl.Float32),
            "low": pl.Series(low, dtype=pl.Float32),
            "close": pl.Series(close, dtype=pl.Float32),
        }
    )


def _run(frame: pl.DataFrame, expr: pl.Expr) -> list[bool]:
    out = frame.select(expr.alias("x"))["x"]
    assert out.dtype == pl.Boolean
    return out.to_list()


def test_doji_is_tiny_body_relative_to_range() -> None:
    # bar0 cuerpo grande (no doji); bar1 open==close (cuerpo 0, doji); bar2 cuerpo 1.5 > 0.1·rango
    frame = _ohlc([100, 100, 100], [101, 101, 102], [99, 99, 98], [100.8, 100.0, 101.5])
    assert _run(frame, doji(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))) == [
        False,
        True,
        False,
    ]


def test_hammer_long_lower_shadow_small_upper() -> None:
    # bar1: cuerpo 0.1 (100->100.1); sombra inf = 100-98 = 2 >= 2·0.1; sombra sup = 0.05
    frame = _ohlc([100, 100.0], [101, 100.15], [99.5, 98.0], [100.5, 100.1])
    assert _run(frame, hammer(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))) == [
        False,
        True,
    ]


def test_shooting_star_long_upper_shadow_small_lower() -> None:
    # bar1: cuerpo 0.1 (100->100.1); sombra sup = 102-100.1 = 1.9 >= 2·0.1; sombra inf = 100-99.95
    frame = _ohlc([100, 100.0], [101, 102.0], [99.5, 99.95], [100.5, 100.1])
    assert _run(
        frame, shooting_star(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))
    ) == [False, True]


def test_marubozu_long_body_no_shadows() -> None:
    # bar1: cuerpo 4 (98->102) = rango; sombras 0 (<= 0.05·rango)
    frame = _ohlc([100, 98.0], [100.8, 102.0], [99.2, 98.0], [100.2, 102.0])
    assert _run(
        frame, marubozu(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))
    ) == [False, True]


def test_bullish_engulfing_wraps_previous_bearish_body() -> None:
    # bar0 bajista (100->98.7); bar1 alcista que envuelve (open 98.5 <= 98.7, close 101.2 >= 100)
    frame = _ohlc([100, 98.5], [100.3, 101.5], [98.5, 98.4], [98.7, 101.2])
    assert _run(
        frame, bullish_engulfing(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))
    ) == [False, True]


def test_bearish_engulfing_wraps_previous_bullish_body() -> None:
    # bar0 alcista (99->101.2); bar1 bajista que envuelve (open 101.3 >= 101.2, close 98.7 <= 99)
    frame = _ohlc([99, 101.3], [101.5, 101.5], [98.8, 98.5], [101.2, 98.7])
    assert _run(
        frame, bearish_engulfing(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))
    ) == [False, True]


def test_harami_small_body_inside_previous_long_body() -> None:
    # bar0 cuerpo largo (103->97.8); bar1 cuerpo chico (99->99.5) dentro de [97.8, 103]
    frame = _ohlc([103, 99.0], [103.3, 99.8], [97.5, 98.6], [97.8, 99.5])
    assert _run(frame, harami(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))) == [
        False,
        True,
    ]


def test_piercing_opens_below_and_closes_above_prev_midpoint() -> None:
    # bar0 bajista (102->98.8), medio=100.4; bar1 abre 98 < low0(98.5), cierra 100.9 en (100.4,102)
    frame = _ohlc([102, 98.0], [102.3, 101.0], [98.5, 97.9], [98.8, 100.9])
    assert _run(
        frame, piercing(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))
    ) == [False, True]


def test_dark_cloud_cover_opens_above_and_closes_below_prev_midpoint() -> None:
    # bar0 alcista (98->101.2), medio=99.6; bar1 abre 102 > high0(101.5), cierra 99 en (98,99.6)
    frame = _ohlc([98, 102.0], [101.5, 102.2], [97.8, 98.9], [101.2, 99.0])
    assert _run(
        frame, dark_cloud_cover(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))
    ) == [False, True]


def test_morning_star_three_bar_bullish_reversal() -> None:
    # bar0 bajista larga (103->97.8); bar1 cuerpo chico con gap (97.3/97.4 < 97.8);
    # bar2 alcista larga (97.6->101.2) que cierra sobre el medio de bar0 (100.4)
    frame = _ohlc([103, 97.3, 97.6], [103.3, 97.6, 101.5], [97.5, 96.8, 97.5], [97.8, 97.4, 101.2])
    out = _run(frame, morning_star(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close")))
    assert out == [False, False, True]


def test_evening_star_three_bar_bearish_reversal() -> None:
    # bar0 alcista larga (97->101.2); bar1 cuerpo chico con gap arriba (101.7/101.9 > 101.2);
    # bar2 bajista larga (101.5->97.8) que cierra bajo el medio de bar0 (99.1)
    frame = _ohlc(
        [97, 101.7, 101.5], [101.5, 102.3, 101.8], [96.8, 101.6, 97.5], [101.2, 101.9, 97.8]
    )
    out = _run(frame, evening_star(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close")))
    assert out == [False, False, True]
