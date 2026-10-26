"""T3.2: propiedad *sin anticipación* / prefijo estable sobre **todo** el catálogo soportado.

Para cada función soportada con sus defaults y ``t`` aleatorio, calcular sobre ``[0..t]`` da
el mismo valor (bit a bit, nulos incluidos) en ``t`` y en todas las filas ``≤ t`` que calcular
sobre la serie completa; alterar filas ``> t`` tampoco cambia el pasado. Las series llevan
nulos y ``NaN`` intercalados (subsecuencia sin nulos, RF-6).

Resultado con ta-lib 0.8.2 / TA-Lib C 0.8.1 (201 funciones, 200 soportadas): **ninguna**
función falla, así que ``catalog.NON_CAUSAL`` está vacío. Incluye ``FRACTAL`` y ``DPO``, que
en su definición clásica usan velas posteriores: TA-Lib las calcula de forma causal (la
señal se publica en la vela que la confirma). Si una versión futura introduce una función
no causal, este test la nombra y debe añadirse a ``NON_CAUSAL`` (``supported=false``,
``unsupported_reason="non_causal"``).

Las comparaciones exactas son válidas porque se comparan dos ejecuciones en la **misma**
máquina (no hay *golden files* entre CPUs distintas).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl
import pytest
from finances_core.schemas.ta_lib import TaLibSpec
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from polars.testing import assert_frame_equal

from finances_analytics.ft_ta_lib import catalog, engine, resolver
from finances_analytics.ft_ta_lib.model import ResolvedFunction

from .conftest import ohlcv_frame

FUNCTIONS: list[ResolvedFunction] = [
    resolver.resolve(TaLibSpec(function=name))
    for name, meta in catalog.function_table().items()
    if meta.supported
]
MAX_LOOKBACK = max(fn.lookback for fn in FUNCTIONS)
COLUMNS = ("open", "high", "low", "close", "volume")


def _with_holes(frame: pl.DataFrame, holes: list[tuple[int, str, bool]]) -> pl.DataFrame:
    out = frame
    for row, column, as_nan in holes:
        if row >= out.height:
            continue
        hole = pl.lit(math.nan if as_nan else None, dtype=pl.Float32)
        out = out.with_columns(
            pl.when(pl.int_range(pl.len()) == row)
            .then(hole)
            .otherwise(pl.col(column))
            .alias(column)
        )
    return out


def _non_causal(frame: pl.DataFrame, t: int, future: pl.DataFrame | None = None) -> list[str]:
    """Funciones cuya salida en ``[0..t]`` cambia al recortar (o alterar) las filas ``> t``."""
    failing: list[str] = []
    other = frame.head(t + 1) if future is None else future
    for fn in FUNCTIONS:
        full = engine.compute_function(frame, fn).frame.head(t + 1)
        cut = engine.compute_function(other, fn).frame.head(t + 1)
        try:
            assert_frame_equal(full, cut, check_exact=True)
        except AssertionError:
            failing.append(fn.name)
    return failing


def test_catalog_is_large_enough_for_the_property() -> None:
    assert len(FUNCTIONS) == 200
    assert MAX_LOOKBACK == 100  # PERCENTRANK


@pytest.mark.parametrize("t", [0, 1, 30, MAX_LOOKBACK, MAX_LOOKBACK + 1, 250, 398])
def test_prefix_is_stable_for_every_function(t: int) -> None:
    frame = _with_holes(ohlcv_frame(400, seed=5), [(120, "close", False), (200, "high", True)])
    assert sorted(_non_causal(frame, t)) == sorted(catalog.NON_CAUSAL)


holes = st.lists(
    st.tuples(st.integers(0, MAX_LOOKBACK + 80), st.sampled_from(COLUMNS), st.booleans()),
    max_size=4,
)


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    n=st.integers(min_value=1, max_value=MAX_LOOKBACK + 80),
    seed=st.integers(min_value=0, max_value=10_000),
    holes_=holes,
    data=st.data(),
)
def test_prefix_is_stable_property(
    n: int, seed: int, holes_: list[tuple[int, str, bool]], data: st.DataObject
) -> None:
    frame = _with_holes(ohlcv_frame(n, seed=seed), holes_)
    t = data.draw(st.integers(min_value=0, max_value=n - 1))
    assert _non_causal(frame, t) == []


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    n=st.integers(min_value=2, max_value=MAX_LOOKBACK + 80),
    seed=st.integers(min_value=0, max_value=10_000),
    data=st.data(),
)
def test_altering_future_rows_does_not_change_the_past(
    n: int, seed: int, data: st.DataObject
) -> None:
    frame = ohlcv_frame(n, seed=seed)
    t = data.draw(st.integers(min_value=0, max_value=n - 2))
    replacement = ohlcv_frame(n, seed=seed + 1).slice(t + 1)
    altered = pl.concat(
        [frame.head(t + 1), replacement.with_columns(frame["open_time_ms"].slice(t + 1))]
    )
    assert _non_causal(frame, t, future=altered) == []


def test_detector_catches_look_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: una salida que depende del futuro (serie invertida) se detecta."""
    real_run = engine._run

    def leaky(fn: ResolvedFunction, arrays: dict[str, np.ndarray[Any, Any]]) -> list[Any]:
        if fn.name == "SMA":
            return [arrays["close"][::-1].copy()]
        return real_run(fn, arrays)

    monkeypatch.setattr(engine, "_run", leaky)
    assert _non_causal(ohlcv_frame(60), 10) == ["SMA"]


def test_non_causal_list_only_names_catalog_functions() -> None:
    table = catalog.function_table()
    for name in catalog.NON_CAUSAL:
        assert table[name].unsupported_reason == "non_causal"
