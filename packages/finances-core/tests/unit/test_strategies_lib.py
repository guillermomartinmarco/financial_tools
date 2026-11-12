"""B1.7–B1.9: ayudas compartidas ``strategies/_lib`` (RF-2, ADR-B12).

Los valores esperados se calculan a mano en cada test (listas literales), nunca llamando
a la implementación. Todas las ayudas devuelven ``Float32``/``Boolean`` (RF-11).
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from finances_core.backtest.strategies._lib import (
    adx,
    atr,
    bbands_bandwidth,
    bbands_pctb,
    cci,
    cross_above,
    cross_below,
    ema,
    ichimoku,
    mfi,
    minus_di,
    obv,
    plus_di,
    roc,
    rolling_max_prev,
    rolling_min_prev,
    rolling_std_stable,
    rsi_wilder,
    sma,
    stoch_d,
    stoch_k,
    vwap,
    williams_r,
    wma,
    zscore,
)
from finances_core.backtest.strategies._lib.stateful import sar, supertrend


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


# ------------------------------------------------------ fundación _lib L1 (Enmienda L1)
def _ohlcv(
    high: list[float], low: list[float], close: list[float], volume: list[float]
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "high": pl.Series(high, dtype=pl.Float32),
            "low": pl.Series(low, dtype=pl.Float32),
            "close": pl.Series(close, dtype=pl.Float32),
            "volume": pl.Series(volume, dtype=pl.Float32),
        }
    )


def test_ema_is_sma_seeded_and_recursive() -> None:
    out = _f32([1.0, 2.0, 3.0, 4.0, 6.0]).select(ema(pl.col("x"), 3)).to_series()
    assert out.dtype == pl.Float32
    # semilla SMA(1,2,3)=2 en t=2; alpha=0.5: t=3 -> 0.5*4+0.5*2=3; t=4 -> 0.5*6+0.5*3=4.5
    assert out.to_list() == [None, None, 2.0, 3.0, 4.5]


def test_rolling_std_stable_is_population_std() -> None:
    out = _f32([1.0, 2.0, 3.0, 4.0, 6.0]).select(rolling_std_stable(pl.col("x"), 3))["x"]
    assert out.dtype == pl.Float32
    # var poblacional: t=2 x=[1,2,3] var=2/3; t=4 x=[3,4,6] mean=13/3 var=14/9
    expected = [None, None, (2 / 3) ** 0.5, (2 / 3) ** 0.5, (14 / 9) ** 0.5]
    assert out.to_list() == pytest.approx(expected, abs=1e-5)


def test_zscore_uses_population_mean_and_std() -> None:
    out = _f32([1.0, 2.0, 3.0, 4.0, 6.0]).select(zscore(pl.col("x"), 3))["x"]
    assert out.dtype == pl.Float32
    # t=2: (3-2)/sqrt(2/3)=1.2247; t=4: (6-13/3)/sqrt(14/9)=1.3363
    expected = [None, None, 1.0 / (2 / 3) ** 0.5, 1.0 / (2 / 3) ** 0.5, (5 / 3) / (14 / 9) ** 0.5]
    assert out.to_list() == pytest.approx(expected, abs=1e-4)


def test_zscore_is_null_when_std_is_zero() -> None:
    out = _f32([5.0, 5.0, 5.0, 5.0]).select(zscore(pl.col("x"), 3))["x"]
    assert out.to_list() == [None, None, None, None]


def test_roc_is_fraction_over_n_bars() -> None:
    out = _f32([1.0, 2.0, 3.0, 4.0, 6.0]).select(roc(pl.col("x"), 2))["x"]
    assert out.dtype == pl.Float32
    # t=2: 3/1-1=2; t=3: 4/2-1=1; t=4: 6/3-1=1
    assert out.to_list() == [None, None, 2.0, 1.0, 1.0]


def test_roc_is_null_when_denominator_is_zero() -> None:
    out = _f32([0.0, 1.0, 2.0, 3.0]).select(roc(pl.col("x"), 2))["x"]
    assert out.to_list() == [None, None, None, 2.0]  # t=2 usa x[0]=0 -> null


def test_wma_weights_are_linear_with_current_bar_heaviest() -> None:
    out = _f32([1.0, 2.0, 3.0, 4.0, 6.0]).select(wma(pl.col("x"), 3)).to_series()
    assert out.dtype == pl.Float32
    # pesos 3,2,1 / 6: t=2 (9+4+1)/6=14/6; t=3 (12+6+2)/6=20/6; t=4 (18+8+3)/6=29/6
    assert out.to_list() == pytest.approx([None, None, 14 / 6, 20 / 6, 29 / 6], abs=1e-5)


def test_atr_is_wilder_mean_of_true_range() -> None:
    frame = _ohlcv(
        [10.0, 11.0, 12.0, 11.0, 13.0],
        [9.0, 10.0, 11.0, 10.0, 12.0],
        [9.5, 10.5, 11.5, 10.5, 13.0],
        [100.0, 200.0, 150.0, 300.0, 250.0],
    )
    out = frame.select(atr(pl.col("high"), pl.col("low"), pl.col("close"), 3)).to_series()
    assert out.dtype == pl.Float32
    # TR = 1, 1.5, 1.5, 1.5, 2.5; Wilder(3): semilla t=3 = 1.5; t=4 = (1.5*2+2.5)/3 = 11/6
    assert out.to_list() == pytest.approx([None, None, None, 1.5, 11 / 6], abs=1e-5)


# ------------------------------------------------- direccional ADX/+DI/−DI (Enmienda L2a)
# Serie corta con n = 2 (convención TA-Lib: semilla direccional de n-1 = 1 término).
#   high:  10  12  11  15  14      low:  8   9   7  12  10     close: 9  11   8  14  11
# t≥1:  up = high-high[t-1], dn = low[t-1]-low; +DM = up si up>dn y up>0; -DM simétrico.
#   t=1: up=2,  dn=-1 -> +DM=2, -DM=0; TR=max(3,|12-9|,|9-9|)=3
#   t=2: up=-1, dn=2  -> +DM=0, -DM=2; TR=max(4,|11-11|,|7-11|)=4
#   t=3: up=4,  dn=-5 -> +DM=4, -DM=0; TR=max(3,|15-8|,|12-8|)=7
#   t=4: up=-1, dn=2  -> +DM=0, -DM=2; TR=max(4,|14-14|,|10-14|)=4
# Suavizado S[1]=v[1] (1 término), S[t]=S[t-1]/2+v[t] (n=2); +DI/-DI = 100·S_dm/S_tr (t≥2).
#   S_+DM: 2, 1, 4.5, 2.25   S_-DM: 0, 2, 1, 2.5   S_TR: 3, 5.5, 9.75, 8.875
#   +DI(t2..4): 100/5.5, 450/9.75, 225/8.875     -DI: 200/5.5, 100/9.75, 250/8.875
# DX = 100·|+DI - -DI|/(+DI + -DI) (t≥2): 33.33333, 63.63636, 5.263158
# ADX = Wilder(DX,2): semilla t=2n-2=2 = DX[2]; primer ADX en t=2n-1=3 = (DX2+DX3)/2.
#   ADX(t3) = (33.33333+63.63636)/2 = 48.484848;  ADX(t4) = (48.484848+5.263158)/2 = 26.874003
DIR_HIGH = [10.0, 12.0, 11.0, 15.0, 14.0]
DIR_LOW = [8.0, 9.0, 7.0, 12.0, 10.0]
DIR_CLOSE = [9.0, 11.0, 8.0, 14.0, 11.0]
DIR_VOL = [100.0, 100.0, 100.0, 100.0, 100.0]


def test_plus_di_is_wilder_ratio_talib_convention() -> None:
    frame = _ohlcv(DIR_HIGH, DIR_LOW, DIR_CLOSE, DIR_VOL)
    out = frame.select(plus_di(pl.col("high"), pl.col("low"), pl.col("close"), 2)).to_series()
    assert out.dtype == pl.Float32
    expected = [None, None, 100 / 5.5, 450 / 9.75, 225 / 8.875]
    assert out.to_list() == pytest.approx(expected, abs=1e-3)


def test_minus_di_is_wilder_ratio_talib_convention() -> None:
    frame = _ohlcv(DIR_HIGH, DIR_LOW, DIR_CLOSE, DIR_VOL)
    out = frame.select(minus_di(pl.col("high"), pl.col("low"), pl.col("close"), 2)).to_series()
    assert out.dtype == pl.Float32
    expected = [None, None, 200 / 5.5, 100 / 9.75, 250 / 8.875]
    assert out.to_list() == pytest.approx(expected, abs=1e-3)


def test_adx_is_double_wilder_of_dx_first_valid_at_2n_minus_1() -> None:
    frame = _ohlcv(DIR_HIGH, DIR_LOW, DIR_CLOSE, DIR_VOL)
    out = frame.select(adx(pl.col("high"), pl.col("low"), pl.col("close"), 2)).to_series()
    assert out.dtype == pl.Float32
    # null para t < 2n-1 = 3; luego el doble Wilder trazado arriba.
    assert out.to_list() == pytest.approx([None, None, None, 48.484848, 26.874003], abs=1e-3)


# ----------------------------------------------------- osciladores L2b (Enmienda L2b)
# high: 10 12 11 13 14   low: 8  9  9 10 11   close: 9 11 10 12 13   vol: 100 200 150 300 250
# tp = (h+l+c)/3:  9,  32/3,  10,  35/3,  38/3
OSC_HIGH = [10.0, 12.0, 11.0, 13.0, 14.0]
OSC_LOW = [8.0, 9.0, 9.0, 10.0, 11.0]
OSC_CLOSE = [9.0, 11.0, 10.0, 12.0, 13.0]
OSC_VOL = [100.0, 200.0, 150.0, 300.0, 250.0]


def test_stoch_k_is_fast_percent_k_over_window() -> None:
    frame = _ohlcv(OSC_HIGH, OSC_LOW, OSC_CLOSE, OSC_VOL)
    out = frame.select(stoch_k(pl.col("high"), pl.col("low"), pl.col("close"), 3)).to_series()
    assert out.dtype == pl.Float32
    # 100·(close-min(low,3))/(max(high,3)-min(low,3)); t=2:(10-8)/4; t=3:(12-9)/4; t=4:(13-9)/5
    assert out.to_list() == pytest.approx([None, None, 50.0, 75.0, 80.0], abs=1e-3)


def test_stoch_d_is_sma_of_stoch_k() -> None:
    frame = _ohlcv(OSC_HIGH, OSC_LOW, OSC_CLOSE, OSC_VOL)
    out = frame.select(stoch_d(pl.col("high"), pl.col("low"), pl.col("close"), 3, 2)).to_series()
    assert out.dtype == pl.Float32
    # SMA(stoch_k,2): t=3 (50+75)/2; t=4 (75+80)/2; null para t < k+d-2 = 3
    assert out.to_list() == pytest.approx([None, None, None, 62.5, 77.5], abs=1e-3)


def test_williams_r_is_negative_range_position() -> None:
    frame = _ohlcv(OSC_HIGH, OSC_LOW, OSC_CLOSE, OSC_VOL)
    out = frame.select(williams_r(pl.col("high"), pl.col("low"), pl.col("close"), 3)).to_series()
    assert out.dtype == pl.Float32
    # -100·(max(high,3)-close)/(max-min); t=2:-(12-10)/4; t=3:-(13-12)/4; t=4:-(14-13)/5
    assert out.to_list() == pytest.approx([None, None, -50.0, -25.0, -20.0], abs=1e-3)


def test_cci_uses_mean_absolute_deviation() -> None:
    frame = _ohlcv(OSC_HIGH, OSC_LOW, OSC_CLOSE, OSC_VOL)
    out = frame.select(cci(pl.col("high"), pl.col("low"), pl.col("close"), 3)).to_series()
    assert out.dtype == pl.Float32
    # (tp - SMA(tp,3)) / (0.015 · MAD), MAD = desviación media ABSOLUTA de la ventana:
    #   t=2: SMA=89/9, MAD=16/27, (1/9)/(0.015·16/27) = 12.5
    #   t=3: SMA=97/9, MAD=16/27, (8/9)/(0.015·16/27) = 100.0
    #   t=4: SMA=103/9, MAD=26/27, (11/9)/(0.015·26/27) = 33/0.39 = 84.615385
    assert out.to_list() == pytest.approx([None, None, 12.5, 100.0, 33 / 0.39], abs=1e-2)


def test_mfi_is_positive_money_flow_ratio_first_valid_at_n() -> None:
    frame = _ohlcv(OSC_HIGH, OSC_LOW, OSC_CLOSE, OSC_VOL)
    out = frame.select(
        mfi(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), 3)
    ).to_series()
    assert out.dtype == pl.Float32
    # raw mf = tp·vol; +si tp sube, -si baja. pos:[-,6400/3,-,3500,9500/3] neg:[-,-,1500,-,-]
    # ventana 3 (incluye t): t=3 pos=16900/3 neg=1500 -> 100·16900/21400 = 78.97196
    #                        t=4 pos=20000/3 neg=1500 -> 100·20000/24500 = 81.63265; null t<n=3
    assert out.to_list() == pytest.approx(
        [None, None, None, 100 * 16900 / 21400, 100 * 20000 / 24500], abs=1e-2
    )


# ----------------------------------------------------- bandas + ichimoku L2c (Enmienda L2c)
def test_bbands_pctb_is_position_within_bands() -> None:
    # close: 1 2 4 7 11, n=3, k=2. mid=SMA(close,3); sd=desv. est. POBLACIONAL (ddof=0).
    #   t=2: mid=7/3,  sd=√14/3 ; t=3: mid=13/3, sd=√38/3 ; t=4: mid=22/3, sd=√74/3
    # %b = (close - (mid - 2sd)) / (4sd)
    frame = _ohlcv(
        [2.0, 3.0, 5.0, 8.0, 12.0],
        [0.0, 1.0, 3.0, 6.0, 10.0],
        [1.0, 2.0, 4.0, 7.0, 11.0],
        [1.0] * 5,
    )
    out = frame.select(bbands_pctb(pl.col("close"), 3, 2.0)).to_series()
    assert out.dtype == pl.Float32
    sd2, sd3, sd4 = math.sqrt(14) / 3, math.sqrt(38) / 3, math.sqrt(74) / 3
    expected = [
        None,
        None,
        (4 - (7 / 3 - 2 * sd2)) / (4 * sd2),
        (7 - (13 / 3 - 2 * sd3)) / (4 * sd3),
        (11 - (22 / 3 - 2 * sd4)) / (4 * sd4),
    ]
    assert out.to_list() == pytest.approx(expected, abs=1e-5)


def test_bbands_bandwidth_is_relative_band_width() -> None:
    # bandwidth = (upper - lower) / mid = 4sd / SMA(close,3), misma serie/sd que %b.
    frame = _ohlcv(
        [2.0, 3.0, 5.0, 8.0, 12.0],
        [0.0, 1.0, 3.0, 6.0, 10.0],
        [1.0, 2.0, 4.0, 7.0, 11.0],
        [1.0] * 5,
    )
    out = frame.select(bbands_bandwidth(pl.col("close"), 3, 2.0)).to_series()
    assert out.dtype == pl.Float32
    sd2, sd3, sd4 = math.sqrt(14) / 3, math.sqrt(38) / 3, math.sqrt(74) / 3
    expected = [None, None, 4 * sd2 / (7 / 3), 4 * sd3 / (13 / 3), 4 * sd4 / (22 / 3)]
    assert out.to_list() == pytest.approx(expected, abs=1e-5)


def test_ichimoku_lines_are_causal_with_backward_shifted_senkou() -> None:
    # tenkan=2, kijun=3, senkou_b=4. high/low de 8 barras; close/vol de relleno.
    high = [10.0, 12.0, 11.0, 13.0, 14.0, 13.0, 15.0, 16.0]
    low = [8.0, 9.0, 9.0, 10.0, 11.0, 10.0, 12.0, 13.0]
    frame = _ohlcv(high, low, high, [1.0] * 8)
    lines = ichimoku(pl.col("high"), pl.col("low"), 2, 3, 4)
    out = frame.select(
        lines["tenkan"].alias("tenkan"),
        lines["kijun"].alias("kijun"),
        lines["senkou_a"].alias("senkou_a"),
        lines["senkou_b"].alias("senkou_b"),
    )
    # tenkan=(max(h,2)+min(l,2))/2 (t>=1); kijun idem con 3 (t>=2)
    assert out["tenkan"].to_list() == pytest.approx(
        [None, 10.0, 10.5, 11.0, 12.0, 12.0, 12.5, 14.0]
    )
    assert out["kijun"].to_list() == pytest.approx([None, None, 10.0, 11.0, 11.5, 12.0, 12.5, 13.0])
    # senkou_a = ((tenkan+kijun)/2).shift(3): raw_a[2..]=10.25,11,11.75 -> en t=5,6,7
    assert out["senkou_a"].to_list() == pytest.approx(
        [None, None, None, None, None, 10.25, 11.0, 11.75]
    )
    # senkou_b = (max(h,4)+min(l,4))/2 .shift(3): raw_b[3..]=10.5,11.5 -> en t=6,7
    assert out["senkou_b"].to_list() == pytest.approx(
        [None, None, None, None, None, None, 10.5, 11.5]
    )


# -------------------------------------------------- con estado L2d (Enmienda L2d)
# high: 10 11 12  9  8   low: 8  9 10  7  6   close: 9 10 11  8  7 ; period=2, mult=1
# TR: 2,2,2,4,2 ; ATR(2): semilla t=2 = (2+2)/2 = 2; t=3 = (2+4)/2 = 3; t=4 = (3+2)/2 = 2.5
STATE_HIGH = [10.0, 11.0, 12.0, 9.0, 8.0]
STATE_LOW = [8.0, 9.0, 10.0, 7.0, 6.0]
STATE_CLOSE = [9.0, 10.0, 11.0, 8.0, 7.0]


def test_supertrend_bands_stick_and_flip_with_trend() -> None:
    hi = pl.Series(STATE_HIGH, dtype=pl.Float32)
    lo = pl.Series(STATE_LOW, dtype=pl.Float32)
    cl = pl.Series(STATE_CLOSE, dtype=pl.Float32)
    st, trend = supertrend(hi, lo, cl, 2, 1.0)
    # supertrend es magnitud (Float32); trend es categórica {-1,+1} -> Int8 (Art. 5.1 bis)
    assert st.dtype == pl.Float32 and trend.dtype == pl.Int8
    # t=2: hl2=11, atr=2 -> fu=13, fl=9; close=11>fl -> alcista, st=fl=9
    # t=3: hl2=8, atr=3 -> fu pega a 11, fl=9; close=8<fl -> baja, st=fu=11
    # t=4: hl2=7, atr=2.5 -> fu=9.5, fl=4.5; sigue baja (close=7<=fu), st=fu=9.5
    assert st.to_list() == pytest.approx([None, None, 9.0, 11.0, 9.5])
    assert trend.to_list() == [None, None, 1, -1, -1]


def test_sar_follows_talib_bootstrap_and_flips() -> None:
    out = sar(
        pl.Series(STATE_HIGH, dtype=pl.Float32), pl.Series(STATE_LOW, dtype=pl.Float32), 0.02, 0.2
    )
    assert out.dtype == pl.Float32
    # init largo (-DM<=0): sar[1]=low[0]=8, ep=high[1]=11, af=0.02
    #   t=1: sin flip, out=8; sar=8+0.02(11-8)=8.06
    #   t=2: high=12>ep -> ep=12,af=0.04; out=8.06; sar=8.06+0.04(12-8.06)=8.2176
    #   t=3: low=7<=8.2176 -> flip corto; out=ep=12 (clamp a rango)
    #   t=4: sin flip corto, out=12
    assert out.to_list() == pytest.approx([None, 8.0, 8.06, 12.0, 12.0], abs=1e-4)


def _causal_prefixes(build, n: int = 90) -> None:
    """Causalidad: ``f(serie[:k])[:k] == f(serie)[:k]`` para todo ``k`` (no mira el futuro)."""
    hi = [100.0 + 10.0 * math.sin(i / 7.0) + (i % 13) * 0.5 for i in range(n)]
    lo = [h - 1.5 - (i % 5) * 0.1 for i, h in enumerate(hi)]
    cl = [h - 0.5 for h in hi]
    full = build(hi, lo, cl)
    for k in (12, 30, 55, 89, n):
        part = build(hi[:k], lo[:k], cl[:k])
        assert part == full[:k], f"prefijo inestable en k={k}"


def test_supertrend_is_causal() -> None:
    def build(hi: list[float], lo: list[float], cl: list[float]) -> list[float | None]:
        st, _ = supertrend(
            pl.Series(hi, dtype=pl.Float32),
            pl.Series(lo, dtype=pl.Float32),
            pl.Series(cl, dtype=pl.Float32),
            10,
            3.0,
        )
        return st.to_list()

    _causal_prefixes(build)


def test_sar_is_causal() -> None:
    def build(hi: list[float], lo: list[float], cl: list[float]) -> list[float | None]:
        return sar(
            pl.Series(hi, dtype=pl.Float32), pl.Series(lo, dtype=pl.Float32), 0.02, 0.2
        ).to_list()

    _causal_prefixes(build)


def test_obv_accumulates_signed_volume_from_zero() -> None:
    frame = _ohlcv(
        [10.0, 11.0, 12.0, 11.0, 13.0],
        [9.0, 10.0, 11.0, 10.0, 12.0],
        [9.5, 10.5, 11.5, 10.5, 13.0],
        [100.0, 200.0, 150.0, 300.0, 250.0],
    )
    out = frame.select(obv(pl.col("close"), pl.col("volume"))).to_series()
    assert out.dtype == pl.Float32
    # 0; +200; +150; -300; +250 -> acumulado
    assert out.to_list() == [0.0, 200.0, 350.0, 50.0, 300.0]


def test_vwap_is_rolling_volume_weighted_typical_price() -> None:
    frame = _ohlcv(
        [10.0, 11.0, 12.0, 11.0, 13.0],
        [9.0, 10.0, 11.0, 10.0, 12.0],
        [9.5, 10.5, 11.5, 10.5, 13.0],
        [100.0, 200.0, 150.0, 300.0, 250.0],
    )
    out = frame.select(
        vwap(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), 3)
    ).to_series()
    assert out.dtype == pl.Float32
    # tp = 9.5, 10.5, 11.5, 10.5, 38/3; t=2: 4775/450; t=3: 6975/650
    expected = [
        None,
        None,
        4775 / 450,
        6975 / 650,
        (11.5 * 150 + 10.5 * 300 + (38 / 3) * 250) / 700,
    ]
    assert out.to_list() == pytest.approx(expected, abs=1e-3)


def test_vwap_is_null_when_window_volume_is_zero() -> None:
    frame = _ohlcv([2.0, 2.0, 2.0], [1.0, 1.0, 1.0], [1.5, 1.5, 1.5], [0.0, 0.0, 0.0])
    out = frame.select(
        vwap(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), 2)
    ).to_series()
    assert out.to_list() == [None, None, None]


# --------------------------------------------------- estabilidad de prefijo (RFC-0006)
def _prefix_frame(n: int = 128) -> pl.DataFrame:
    # Serie determinista no trivial (sin numpy); volumen > 0 estrictamente.
    close = [100.0 + 10.0 * math.sin(i / 7.0) + (i % 13) * 0.5 for i in range(n)]
    high = [c + 1.25 + (i % 5) * 0.1 for i, c in enumerate(close)]
    low = [c - 1.25 - (i % 7) * 0.1 for i, c in enumerate(close)]
    volume = [100.0 + (i % 17) * 10.0 for i in range(n)]
    return _ohlcv(high, low, close, volume)


_PREFIX_PRIMITIVES = {
    "sma": lambda: sma(pl.col("close"), 12),
    "ema": lambda: ema(pl.col("close"), 12),
    "wma": lambda: wma(pl.col("close"), 12),
    "rsi_wilder": lambda: rsi_wilder(pl.col("close"), 14),
    "rolling_std_stable": lambda: rolling_std_stable(pl.col("close"), 20),
    "zscore": lambda: zscore(pl.col("close"), 20),
    "roc": lambda: roc(pl.col("close"), 10),
    "atr": lambda: atr(pl.col("high"), pl.col("low"), pl.col("close"), 14),
    "obv": lambda: obv(pl.col("close"), pl.col("volume")),
    "vwap": lambda: vwap(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), 20),
    "rolling_max_prev": lambda: rolling_max_prev(pl.col("high"), 20),
    "rolling_min_prev": lambda: rolling_min_prev(pl.col("low"), 20),
    "plus_di": lambda: plus_di(pl.col("high"), pl.col("low"), pl.col("close"), 14),
    "minus_di": lambda: minus_di(pl.col("high"), pl.col("low"), pl.col("close"), 14),
    "adx": lambda: adx(pl.col("high"), pl.col("low"), pl.col("close"), 14),
    "stoch_k": lambda: stoch_k(pl.col("high"), pl.col("low"), pl.col("close"), 5),
    "stoch_d": lambda: stoch_d(pl.col("high"), pl.col("low"), pl.col("close"), 5, 3),
    "williams_r": lambda: williams_r(pl.col("high"), pl.col("low"), pl.col("close"), 14),
    "cci": lambda: cci(pl.col("high"), pl.col("low"), pl.col("close"), 14),
    "mfi": lambda: mfi(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), 14),
    "bbands_pctb": lambda: bbands_pctb(pl.col("close"), 20, 2.0),
    "bbands_bandwidth": lambda: bbands_bandwidth(pl.col("close"), 20, 2.0),
    "ichimoku_tenkan": lambda: ichimoku(pl.col("high"), pl.col("low"), 9, 26, 52)["tenkan"],
    "ichimoku_senkou_a": lambda: ichimoku(pl.col("high"), pl.col("low"), 9, 26, 52)["senkou_a"],
    "ichimoku_senkou_b": lambda: ichimoku(pl.col("high"), pl.col("low"), 9, 26, 52)["senkou_b"],
}


@pytest.mark.parametrize("name", sorted(_PREFIX_PRIMITIVES))
def test_primitive_is_prefix_stable_bit_for_bit(name: str) -> None:
    # f(x[:k]) == f(x)[:k] exacto para todo k: ninguna primitiva mira barras futuras
    # ni depende del largo total (rolling_std/var de Polars fallarían este test; #50/RFC-0006).
    frame = _prefix_frame()
    build = _PREFIX_PRIMITIVES[name]
    full = frame.select(build()).to_series().to_list()
    for k in (21, 40, 64, 97, frame.height - 1, frame.height):
        part = frame.head(k).select(build()).to_series().to_list()
        assert part == full[:k], f"{name}: prefijo inestable en k={k}"
