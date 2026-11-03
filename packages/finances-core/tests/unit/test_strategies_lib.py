"""B1.7–B1.9: ayudas compartidas ``strategies/_lib`` (RF-2, ADR-B12).

Los valores esperados se calculan a mano en cada test (listas literales), nunca llamando
a la implementación. Todas las ayudas devuelven ``Float32``/``Boolean`` (RF-11).
"""

from __future__ import annotations

import polars as pl
import pytest

from finances_core.backtest.strategies._lib import (
    cross_above,
    cross_below,
    rolling_max_prev,
    rolling_min_prev,
    rsi_wilder,
    sma,
)


def _f32(values: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"x": pl.Series(values, dtype=pl.Float32)})


def _pair(a: list[float], b: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"a": pl.Series(a, dtype=pl.Float32), "b": pl.Series(b, dtype=pl.Float32)})


# ------------------------------------------------------------------------ sma (B1.7)
def test_sma_is_arithmetic_mean_defined_from_n_minus_1() -> None:
    out = _f32([1.0, 2.0, 3.0, 4.0, 6.0]).select(sma(pl.col("x"), 3))["x"]
    assert out.dtype == pl.Float32
    # SMA_3(t) = (x[t-2] + x[t-1] + x[t]) / 3, definida para t >= 2
    assert out.to_list() == [None, None, 2.0, 3.0, pytest.approx(13 / 3)]


# --------------------------------------------------------------- cruces (B1.7)
def test_cross_above_is_strict_and_accepts_previous_tie() -> None:
    # a - b:            -1   0   1   1  -1   0   0
    a = [1.0, 2.0, 4.0, 5.0, 3.0, 5.0, 5.0]
    b = [2.0, 2.0, 3.0, 4.0, 4.0, 5.0, 5.0]
    out = _pair(a, b).select(cross_above(pl.col("a"), pl.col("b")))["a"]
    assert out.dtype == pl.Boolean
    # t=0 sin barra previa -> false; t=2: 0 <= 0 y 1 > 0; empates en t=1, 5, 6 sin señal
    assert out.to_list() == [False, False, True, False, False, False, False]


def test_cross_below_is_strict_and_accepts_previous_tie() -> None:
    # a - b:             1   0  -1  -1   1   0
    a = [3.0, 2.0, 1.0, 1.0, 5.0, 4.0]
    b = [2.0, 2.0, 2.0, 2.0, 4.0, 4.0]
    out = _pair(a, b).select(cross_below(pl.col("a"), pl.col("b")))["a"]
    assert out.dtype == pl.Boolean
    assert out.to_list() == [False, False, True, False, False, False]


def test_cross_against_a_constant_threshold() -> None:
    x = [50.0, 40.0, 29.0, 30.0, 25.0, 71.0, 70.0, 75.0]
    frame = _f32(x)
    below = frame.select(cross_below(pl.col("x"), 30.0))["x"].to_list()
    above = frame.select(cross_above(pl.col("x"), 70.0))["x"].to_list()
    # 40 >= 30 y 29 < 30 -> t=2; 30 >= 30 y 25 < 30 -> t=4 (el empate previo cuenta)
    assert below == [False, False, True, False, True, False, False, False]
    # 25 <= 70 y 71 > 70 -> t=5; 70 <= 70 y 75 > 70 -> t=7
    assert above == [False, False, False, False, False, True, False, True]


def test_cross_with_missing_data_is_false_not_null() -> None:
    frame = _pair([None, 1.0, 3.0], [None, 2.0, 2.0])  # type: ignore[list-item]
    out = frame.select(cross_above(pl.col("a"), pl.col("b")))["a"]
    assert out.null_count() == 0
    assert out.to_list() == [False, False, True]


# --------------------------------------------------------------- rsi_wilder (B1.8)
# close:   10  11  12  11   9  10  13  13  13  10
# Δ:        —   1   1  -1  -2   1   3   0   0  -3
# period = 2. Semilla en t=2: AG = (1+1)/2 = 1, AL = (0+0)/2 = 0 -> RSI 100.
# Wilder: media[t] = (media[t-1]·1 + valor[t]) / 2.
#   t=3: AG 0.5,      AL 0.5       RS 1     -> 50
#   t=4: AG 0.25,     AL 1.25      RS 0.2   -> 100 - 100/1.2   = 16.6667
#   t=5: AG 0.625,    AL 0.625     RS 1     -> 50
#   t=6: AG 1.8125,   AL 0.3125    RS 5.8   -> 100 - 100/6.8   = 85.2941
#   t=7: AG 0.90625,  AL 0.15625   RS 5.8   -> 85.2941
#   t=8: AG 0.453125, AL 0.078125  RS 5.8   -> 85.2941
#   t=9: AG 0.2265625, AL 1.5390625 RS 29/197 -> 100·29/226 = 12.8319
RSI_CLOSES = [10.0, 11.0, 12.0, 11.0, 9.0, 10.0, 13.0, 13.0, 13.0, 10.0]
RSI_EXPECTED = [
    None,
    None,
    100.0,
    50.0,
    100 - 100 / 1.2,
    50.0,
    100 - 100 / 6.8,
    100 - 100 / 6.8,
    100 - 100 / 6.8,
    100 * 29 / 226,
]


def _rsi(values: list[float], period: int) -> list[float | None]:
    out = _f32(values).select(rsi=rsi_wilder(pl.col("x"), period))["rsi"]
    assert out.dtype == pl.Float32
    return out.to_list()


def test_rsi_wilder_seed_at_period_and_recursion() -> None:
    got = _rsi(RSI_CLOSES, 2)
    assert got[:2] == [None, None]  # definido para t >= period
    assert got[2:] == pytest.approx(RSI_EXPECTED[2:], abs=1e-4)


def test_rsi_wilder_seed_is_simple_mean_of_first_period_changes() -> None:
    # period = 3: Δ[1..3] = 2, -1, 2 -> AG = 4/3, AL = 1/3, RS = 4 -> RSI 80 en t=3
    got = _rsi([10.0, 12.0, 11.0, 13.0], 3)
    assert got[:3] == [None, None, None]
    assert got[3] == pytest.approx(80.0, abs=1e-4)


def test_rsi_wilder_flat_series_is_50() -> None:
    assert _rsi([5.0] * 6, 2) == [None, None, 50.0, 50.0, 50.0, 50.0]


def test_rsi_wilder_only_gains_is_100() -> None:
    assert _rsi([1.0, 2.0, 3.0, 4.0, 5.0], 2) == [None, None, 100.0, 100.0, 100.0]


def test_rsi_wilder_only_losses_is_0() -> None:
    assert _rsi([5.0, 4.0, 3.0, 2.0], 2) == [None, None, 0.0, 0.0]


# ---------------------------------------------------- canal previo (B1.9)
def test_rolling_max_and_min_prev_exclude_current_bar() -> None:
    frame = _f32([3.0, 1.0, 4.0, 1.0, 5.0, 9.0])
    out = frame.select(hi=rolling_max_prev(pl.col("x"), 3), lo=rolling_min_prev(pl.col("x"), 3))
    assert out["hi"].dtype == pl.Float32
    assert out["lo"].dtype == pl.Float32
    # t=3: x[0..2] = 3, 1, 4; t=4: 1, 4, 1; t=5: 4, 1, 5 (nunca incluye x[t])
    assert out["hi"].to_list() == [None, None, None, 4.0, 4.0, 5.0]
    assert out["lo"].to_list() == [None, None, None, 1.0, 1.0, 1.0]
