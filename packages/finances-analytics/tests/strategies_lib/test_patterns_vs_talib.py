"""L2e — cross-validación viva de los patrones de velas de ``_lib`` vs los ``CDL*`` de TA-Lib.

Los ``CDL*`` de TA-Lib usan umbrales adaptativos (promedios de cuerpo/rango), así que el acuerdo
exacto sobre ruido aleatorio no es un objetivo. Acá se validan los patrones sobre **casos de
libro**: una antesala (plana, alcista o bajista según lo exija TA-Lib) seguida de la(s) vela(s)
del patrón; en la barra señal se exige que **nuestro booleano sea** ``True`` **y** que el ``CDL*``
dispare con el signo esperado (``>0`` alcista, ``<0`` bajista, ``!=0`` no direccional). El
*golden* hand-computed de cada patrón vive en finances-core.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import talib
from finances_core.backtest.strategies._lib import patterns as pat

from ..test_talib_version_guard import check_talib_versions

Bar = tuple[float, float, float, float]  # (open, high, low, close)


def _flat(n: int = 16) -> list[Bar]:
    return [(100.0, max(100.0, c) + 0.3, min(100.0, c) - 0.3, c) for c in _flat_closes(n)]


def _flat_closes(n: int) -> list[float]:
    return [100.4 if i % 2 else 99.6 for i in range(n)]


def _downtrend(n: int = 16) -> list[Bar]:
    bars = []
    price = 110.0
    for _ in range(n):
        o, c = price, price - 1.0
        bars.append((o, o + 0.2, c - 0.2, c))
        price -= 1.0
    return bars


def _uptrend(n: int = 16) -> list[Bar]:
    bars = []
    price = 90.0
    for _ in range(n):
        o, c = price, price + 1.0
        bars.append((o, c + 0.2, o - 0.2, c))
        price += 1.0
    return bars


#: (nombre, función de ``_lib``, función ``CDL*``, antesala, velas del patrón, signo esperado).
CASES: list[tuple[str, str, str, list[Bar], list[Bar], str]] = [
    ("doji", "doji", "CDLDOJI", _flat(), [(100.0, 101.0, 99.0, 100.0)], "nonzero"),
    ("hammer", "hammer", "CDLHAMMER", _downtrend(), [(94.0, 94.15, 91.5, 94.1)], "pos"),
    (
        "shooting_star",
        "shooting_star",
        "CDLSHOOTINGSTAR",
        _uptrend(),
        [(108.0, 110.0, 107.9, 108.1)],
        "neg",
    ),
    ("marubozu", "marubozu", "CDLMARUBOZU", _flat(), [(98.0, 102.0, 97.98, 102.0)], "nonzero"),
    (
        "bullish_engulfing",
        "bullish_engulfing",
        "CDLENGULFING",
        _flat(),
        [(100.0, 100.3, 98.5, 98.7), (98.5, 101.5, 98.4, 101.2)],
        "pos",
    ),
    (
        "bearish_engulfing",
        "bearish_engulfing",
        "CDLENGULFING",
        _flat(),
        [(99.0, 101.5, 98.8, 101.2), (101.3, 101.5, 98.5, 98.7)],
        "neg",
    ),
    (
        "harami",
        "harami",
        "CDLHARAMI",
        _flat(),
        [(103.0, 103.3, 97.5, 97.8), (99.0, 99.8, 98.6, 99.5)],
        "nonzero",
    ),
    (
        "piercing",
        "piercing",
        "CDLPIERCING",
        _flat(),
        [(102.0, 102.3, 98.5, 98.8), (98.0, 101.0, 97.9, 100.9)],
        "pos",
    ),
    (
        "dark_cloud_cover",
        "dark_cloud_cover",
        "CDLDARKCLOUDCOVER",
        _flat(),
        [(98.0, 101.5, 97.8, 101.2), (102.0, 102.2, 98.9, 99.0)],
        "neg",
    ),
    (
        "morning_star",
        "morning_star",
        "CDLMORNINGSTAR",
        _flat(),
        [(103.0, 103.3, 97.5, 97.8), (97.3, 97.6, 96.8, 97.4), (97.6, 101.5, 97.5, 101.2)],
        "pos",
    ),
    (
        "evening_star",
        "evening_star",
        "CDLEVENINGSTAR",
        _flat(),
        [(97.0, 101.5, 96.8, 101.2), (101.7, 102.3, 101.6, 101.9), (101.5, 101.8, 97.5, 97.8)],
        "neg",
    ),
]


def _columns(bars: list[Bar]) -> dict[str, list[float]]:
    return {
        "open": [b[0] for b in bars],
        "high": [b[1] for b in bars],
        "low": [b[2] for b in bars],
        "close": [b[3] for b in bars],
    }


def test_talib_is_the_validated_version() -> None:
    check_talib_versions(talib)


@pytest.mark.parametrize("name,my_fn,cdl,warmup,pattern,sign", CASES, ids=[c[0] for c in CASES])
def test_pattern_matches_talib_on_textbook_case(
    name: str,
    my_fn: str,
    cdl: str,
    warmup: list[Bar],
    pattern: list[Bar],
    sign: str,
) -> None:
    cols = _columns(warmup + pattern)
    signal = len(warmup + pattern) - 1

    frame = pl.DataFrame({k: pl.Series(v, dtype=pl.Float32) for k, v in cols.items()})
    expr = getattr(pat, my_fn)(pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"))
    ours = frame.select(expr.alias("x"))["x"].to_list()

    ref = getattr(talib, cdl)(
        *(np.array(cols[c], dtype=float) for c in ("open", "high", "low", "close"))
    )

    assert ours[signal] is True, f"{name}: _lib no detectó el patrón en la barra señal"
    if sign == "pos":
        assert ref[signal] > 0, f"{name}: CDL {cdl}={ref[signal]} (esperado alcista)"
    elif sign == "neg":
        assert ref[signal] < 0, f"{name}: CDL {cdl}={ref[signal]} (esperado bajista)"
    else:
        assert ref[signal] != 0, f"{name}: CDL {cdl}=0 (esperado patrón no direccional)"
