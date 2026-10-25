"""T3.1: propiedad *sin anticipación* / prefijo estable para todo el catálogo (RF-3, US-2).

Sobre series aleatorias con nulos, ``NaN``, ``±inf`` y ceros intercalados:

- para todo ``t``, calcular sobre ``[0..t]`` da el mismo valor **bit a bit** en ``t`` (y en
  todas las filas ``≤ t``) que calcular sobre la serie completa;
- alterar filas ``> t`` no cambia la salida en ``≤ t``.

Se comprueba sobre las salidas ``Float64`` del oráculo (más estricto que ``Float32``).
"""

from __future__ import annotations

import math

import polars as pl
from finances_core.schemas.indicators import IndicatorSpec
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from polars.testing import assert_frame_equal

from finances_analytics.ft_indicators import engine
from finances_analytics.ft_indicators.catalog import CATALOG, ResolvedIndicator, resolve

from .helpers import ohlcv_frame

#: Parámetros pequeños para salir del calentamiento con series cortas + los defaults.
SMALL = [
    {"name": "sma", "params": {"period": 3}},
    {"name": "ema", "params": {"period": 3}},
    {"name": "wma", "params": {"period": 4}},
    {"name": "rsi", "params": {"period": 3}},
    {"name": "macd", "params": {"fast": 2, "slow": 4, "signal": 2}},
    {"name": "bb", "params": {"period": 3, "std": 1.5}},
    {"name": "atr", "params": {"period": 3}},
    {"name": "stoch", "params": {"k": 3, "smooth_k": 2, "d": 2}},
    {"name": "roc", "params": {"period": 2}},
    {"name": "obv"},
    {"name": "adx", "params": {"period": 3}},
    {"name": "vwap", "params": {"period": 3}},
]
INDICATORS: list[ResolvedIndicator] = [
    *(resolve(IndicatorSpec.model_validate(s)) for s in SMALL),
    *(resolve(IndicatorSpec.model_validate({"name": n})) for n in CATALOG),
]
COLUMNS = ("open", "high", "low", "close", "volume")

values = st.one_of(
    st.floats(min_value=1.0, max_value=1_000.0),
    st.floats(min_value=1.0, max_value=1_000.0),
    st.floats(min_value=1.0, max_value=1_000.0),
    st.just(0.0),
    st.none(),
    st.just(math.nan),
    st.just(math.inf),
)


@st.composite
def series(draw: st.DrawFn, min_size: int = 1, max_size: int = 40) -> pl.DataFrame:
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    data = {c: draw(st.lists(values, min_size=n, max_size=n)) for c in COLUMNS}
    return ohlcv_frame(data)


def _oracle(frame: pl.DataFrame) -> pl.DataFrame:
    return engine.compute_float64(frame, INDICATORS)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(series())
def test_prefix_is_stable_bit_for_bit(frame: pl.DataFrame) -> None:
    full = _oracle(frame)
    for t in range(frame.height):
        prefix = _oracle(frame.head(t + 1))
        assert_frame_equal(prefix, full.head(t + 1), check_exact=True)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(series(min_size=2), st.data())
def test_altering_future_rows_does_not_change_the_past(
    frame: pl.DataFrame, data: st.DataObject
) -> None:
    t = data.draw(st.integers(min_value=0, max_value=frame.height - 2))
    tail = frame.height - t - 1
    replacement = {c: data.draw(st.lists(values, min_size=tail, max_size=tail)) for c in COLUMNS}
    altered = pl.concat(
        [
            frame.head(t + 1),
            ohlcv_frame(replacement).with_columns(frame["open_time_ms"].tail(tail)),
        ]
    )
    before = _oracle(frame).head(t + 1)
    after = _oracle(altered).head(t + 1)
    assert_frame_equal(before, after, check_exact=True)
