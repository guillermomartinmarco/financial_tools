"""L2.oracle — auto-prueba del harness de cross-validación viva de ``_lib`` vs TA-Lib.

Antes de validar un solo indicador de L2, se prueba el **instrumento de medición**: la serie
determinista, la alineación de calentamiento (``null`` ↔ ``NaN``) y la tolerancia ``Float32``
se validan contra las primitivas de ``_lib`` que **ya existen** (L1) y tienen equivalente
TA-Lib exacto (``sma``/``SMA``, ``ema``/``EMA``, ``wma``/``WMA``, ``rsi_wilder``/``RSI``,
``atr``/``ATR``). Si estas concuerdan dentro de tolerancia, el harness es confiable y cada
indicador de L2 se enchufa al mismo ``assert_matches``.

``obv`` queda fuera a propósito: TA-Lib inicializa ``OBV[0] = volume[0]`` y ``_lib`` en ``0``
(diferencia de offset constante documentada en ``_lib.obv``), así que no es un equivalente
absoluto. ``vwap``/``roc``/``zscore`` tampoco tienen equivalente TA-Lib directo con los mismos
defaults; se cubren por *golden* en finances-core.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import talib
from finances_core.backtest.strategies._lib import (
    adx,
    atr,
    bbands_bandwidth,
    bbands_pctb,
    cci,
    ema,
    ichimoku,
    mfi,
    minus_di,
    plus_di,
    rsi_wilder,
    sma,
    stoch_d,
    stoch_k,
    williams_r,
    wma,
)
from finances_core.backtest.strategies._lib.stateful import sar, supertrend

from ..test_talib_version_guard import check_talib_versions
from ._oracle import assert_matches, ohlcv, price_scale, talib_inputs


@pytest.fixture(scope="module")
def series() -> dict[str, pl.Series]:
    return ohlcv()


@pytest.fixture(scope="module")
def refs(series: dict[str, pl.Series]) -> dict[str, np.ndarray]:
    return talib_inputs(series)


def _apply(series: dict[str, pl.Series], expr: pl.Expr) -> pl.Series:
    """Evalúa una ``pl.Expr`` de ``_lib`` sobre la serie y devuelve la columna resultante."""
    return pl.DataFrame(dict(series)).select(expr.alias("out"))["out"]


def test_talib_is_the_validated_version() -> None:
    """El oráculo solo vale contra la versión de TA-Lib validada (RF-19)."""
    check_talib_versions(talib)


# ------------------------------------------------------------------ serie determinista


def test_ohlcv_series_is_valid_and_deterministic() -> None:
    first = ohlcv()
    second = ohlcv()
    for name, col in first.items():
        assert col.dtype == pl.Float32
        assert col.to_list() == second[name].to_list(), f"{name} no es determinista"
    checks = pl.DataFrame(dict(first)).select(
        (pl.col("high") >= pl.max_horizontal("open", "close")).all().alias("high_ok"),
        (pl.col("low") <= pl.min_horizontal("open", "close")).all().alias("low_ok"),
        (pl.col("volume") > 0).all().alias("vol_ok"),
    )
    assert checks["high_ok"][0], "high < max(open, close)"
    assert checks["low_ok"][0], "low > min(open, close)"
    assert checks["vol_ok"][0], "volume no positivo"


# ------------------------------------------------------------------ auto-prueba vs TA-Lib


def test_sma_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 20
    ours = _apply(series, sma(pl.col("close"), n))
    assert_matches(ours, talib.SMA(refs["close"], timeperiod=n), compare_from=n - 1)


def test_ema_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 20
    ours = _apply(series, ema(pl.col("close"), n))
    assert_matches(ours, talib.EMA(refs["close"], timeperiod=n), compare_from=n - 1)


def test_wma_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 20
    ours = _apply(series, wma(pl.col("close"), n))
    assert_matches(ours, talib.WMA(refs["close"], timeperiod=n), compare_from=n - 1)


def test_rsi_wilder_matches_talib(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    n = 14
    ours = _apply(series, rsi_wilder(pl.col("close"), n))
    # RSI en [0, 100]: piso atol fijo (escala del oscilador), no de precio.
    assert_matches(ours, talib.RSI(refs["close"], timeperiod=n), compare_from=n, atol=2e-3)


def test_atr_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 14
    ours = _apply(series, atr(pl.col("high"), pl.col("low"), pl.col("close"), n))
    scale = price_scale(series)
    assert_matches(
        ours,
        talib.ATR(refs["high"], refs["low"], refs["close"], timeperiod=n),
        compare_from=n,
        atol=1e-3 * scale,
    )


# ------------------------------------------------------ L2a direccional (ADX/+DI/−DI)


def test_plus_di_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 14
    ours = _apply(series, plus_di(pl.col("high"), pl.col("low"), pl.col("close"), n))
    # +DI en [0, 100]: primer valor en t=n (convención TA-Lib, semilla de n-1 términos).
    assert_matches(
        ours,
        talib.PLUS_DI(refs["high"], refs["low"], refs["close"], timeperiod=n),
        compare_from=n,
        atol=3e-3,
    )


def test_minus_di_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 14
    ours = _apply(series, minus_di(pl.col("high"), pl.col("low"), pl.col("close"), n))
    assert_matches(
        ours,
        talib.MINUS_DI(refs["high"], refs["low"], refs["close"], timeperiod=n),
        compare_from=n,
        atol=3e-3,
    )


def test_adx_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 14
    ours = _apply(series, adx(pl.col("high"), pl.col("low"), pl.col("close"), n))
    # ADX en [0, 100]: doble Wilder, primer valor en t=2n-1.
    assert_matches(
        ours,
        talib.ADX(refs["high"], refs["low"], refs["close"], timeperiod=n),
        compare_from=2 * n - 1,
        atol=3e-3,
    )


# ------------------------------------------------------ L2b osciladores (stoch/cci/willr/mfi)


def test_stoch_k_matches_talib_fastk(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    k, d = 5, 3
    ours = _apply(series, stoch_k(pl.col("high"), pl.col("low"), pl.col("close"), k))
    fastk, _ = talib.STOCHF(
        refs["high"], refs["low"], refs["close"], fastk_period=k, fastd_period=d, fastd_matype=0
    )
    # STOCHF expone fastk recién desde k+d-2 (quirk de TA-Lib); el borde k-1 lo cubre el golden.
    assert_matches(ours, fastk, compare_from=k + d - 2, atol=3e-3)


def test_stoch_d_matches_talib_fastd(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    k, d = 5, 3
    ours = _apply(series, stoch_d(pl.col("high"), pl.col("low"), pl.col("close"), k, d))
    _, fastd = talib.STOCHF(
        refs["high"], refs["low"], refs["close"], fastk_period=k, fastd_period=d, fastd_matype=0
    )
    assert_matches(ours, fastd, compare_from=k + d - 2, atol=3e-3)


def test_cci_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 14
    ours = _apply(series, cci(pl.col("high"), pl.col("low"), pl.col("close"), n))
    # CCI tiene rango amplio (±cientos); la tolerancia relativa domina.
    assert_matches(
        ours,
        talib.CCI(refs["high"], refs["low"], refs["close"], timeperiod=n),
        compare_from=n - 1,
        rtol=1e-3,
        atol=1e-2,
    )


def test_williams_r_matches_talib(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    n = 14
    ours = _apply(series, williams_r(pl.col("high"), pl.col("low"), pl.col("close"), n))
    assert_matches(
        ours,
        talib.WILLR(refs["high"], refs["low"], refs["close"], timeperiod=n),
        compare_from=n - 1,
        atol=3e-3,
    )


def test_mfi_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    n = 14
    ours = _apply(series, mfi(pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"), n))
    assert_matches(
        ours,
        talib.MFI(refs["high"], refs["low"], refs["close"], refs["volume"], timeperiod=n),
        compare_from=n,
        atol=3e-3,
    )


# ------------------------------------------------------ L2c bandas + ichimoku (causal)


def _shift_forward(ref: np.ndarray, by: int) -> np.ndarray:
    """``out[t] = ref[t-by]`` con ``NaN`` al inicio (alinea el senkou causal con TA-Lib crudo)."""
    out = np.full_like(ref, np.nan)
    out[by:] = ref[:-by]
    return out


def test_bbands_pctb_matches_talib(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    n, k = 20, 2.0
    ours = _apply(series, bbands_pctb(pl.col("close"), n, k))
    upper, _, lower = talib.BBANDS(refs["close"], timeperiod=n, nbdevup=k, nbdevdn=k, matype=0)
    pctb = (refs["close"] - lower) / (upper - lower)
    assert_matches(ours, pctb, compare_from=n - 1, rtol=1e-3, atol=1e-3)


def test_bbands_bandwidth_matches_talib(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    n, k = 20, 2.0
    ours = _apply(series, bbands_bandwidth(pl.col("close"), n, k))
    upper, middle, lower = talib.BBANDS(refs["close"], timeperiod=n, nbdevup=k, nbdevdn=k, matype=0)
    bandwidth = (upper - lower) / middle
    assert_matches(ours, bandwidth, compare_from=n - 1, rtol=1e-3, atol=1e-4)


def test_ichimoku_tenkan_kijun_match_talib(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    tenkan_p, kijun_p, senkou_b_p = 9, 26, 52
    lines = ichimoku(pl.col("high"), pl.col("low"), tenkan_p, kijun_p, senkou_b_p)
    tenkansen, kijunsen, _, _ = talib.ICHIMOKU(
        refs["high"],
        refs["low"],
        tenkanperiod=tenkan_p,
        kijunperiod=kijun_p,
        senkoubperiod=senkou_b_p,
    )
    # TA-Lib expone todas las salidas recién desde senkou_b-1 (la ventana más larga).
    assert_matches(_apply(series, lines["tenkan"]), tenkansen, compare_from=senkou_b_p - 1)
    assert_matches(_apply(series, lines["kijun"]), kijunsen, compare_from=senkou_b_p - 1)


def test_ichimoku_senkou_are_causal_shift_of_talib(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    tenkan_p, kijun_p, senkou_b_p = 9, 26, 52
    lines = ichimoku(pl.col("high"), pl.col("low"), tenkan_p, kijun_p, senkou_b_p)
    _, _, span_a, span_b = talib.ICHIMOKU(
        refs["high"],
        refs["low"],
        tenkanperiod=tenkan_p,
        kijunperiod=kijun_p,
        senkoubperiod=senkou_b_p,
    )
    # nuestro senkou[t] = ((tenkan+kijun)/2 ó midchan)[t-kijun] = TA-Lib span crudo[t-kijun].
    cf = senkou_b_p - 1 + kijun_p
    assert_matches(
        _apply(series, lines["senkou_a"]), _shift_forward(span_a, kijun_p), compare_from=cf
    )
    assert_matches(
        _apply(series, lines["senkou_b"]), _shift_forward(span_b, kijun_p), compare_from=cf
    )


# ------------------------------------------------------ L2d con estado (supertrend/sar)


def test_supertrend_matches_talib(
    series: dict[str, pl.Series], refs: dict[str, np.ndarray]
) -> None:
    period, mult = 10, 3.0
    st, trend = supertrend(series["high"], series["low"], series["close"], period, mult)
    ref_st, ref_trend = talib.SUPERTREND(
        refs["high"], refs["low"], refs["close"], timeperiod=period, multiplier=mult
    )
    scale = price_scale(series)
    assert_matches(st, ref_st, compare_from=period, atol=1e-3 * scale)
    # trend es categórica Int8 {-1,+1} (Art. 5.1 bis): comparación exacta desde t=period.
    assert trend.dtype == pl.Int8
    ours_trend = trend.to_list()
    for t in range(period, len(ours_trend)):
        assert ours_trend[t] == int(ref_trend[t]), f"trend difiere en t={t}"


def test_sar_matches_talib(series: dict[str, pl.Series], refs: dict[str, np.ndarray]) -> None:
    out = sar(series["high"], series["low"], 0.02, 0.2)
    ref = talib.SAR(refs["high"], refs["low"], acceleration=0.02, maximum=0.2)
    scale = price_scale(series)
    assert_matches(out, ref, compare_from=1, atol=1e-3 * scale)
