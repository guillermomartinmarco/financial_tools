"""T2.5: kernels recursivos (``ema``, ``rsi``, ``macd``, ``atr``, ``adx``)."""

from __future__ import annotations

import math

import polars as pl
import pytest

from finances_analytics.ft_indicators.kernels import recursive
from finances_analytics.ft_indicators.kernels.common import Params, Program

from .helpers import (
    KernelFn,
    apply_kernel,
    as_floats,
    float64_frame,
    ohlcv_frame,
    random_walk,
    read_reference,
)


def _assert_close(got: list[float | None], expected: list[float | None], rtol: float) -> None:
    assert len(got) == len(expected)
    for i, (g, e) in enumerate(zip(got, expected, strict=True)):
        if e is None:
            assert g is None, f"fila {i}: se esperaba nulo, hay {g}"
        else:
            assert g is not None, f"fila {i}: nulo inesperado (se esperaba {e})"
            assert math.isclose(g, e, rel_tol=rtol, abs_tol=0.0), f"fila {i}: {g} != {e}"


# ------------------------------------------------------------------ fixtures publicados


def test_ema_matches_stockcharts_sheet() -> None:
    ref = read_reference("stockcharts_sma_ema_10.csv")
    frame = float64_frame({"close": as_floats(ref, "close")})
    out = apply_kernel(recursive.ema, {"period": 10, "source": "close"}, frame)
    _assert_close(out["value"].to_list(), as_floats(ref, "ema_10"), 1e-9)


def test_rsi_matches_stockcharts_sheet() -> None:
    ref = read_reference("stockcharts_rsi_14.csv")
    frame = float64_frame({"close": as_floats(ref, "close")})
    out = apply_kernel(recursive.rsi, {"period": 14, "source": "close"}, frame)
    _assert_close(out["value"].to_list(), as_floats(ref, "rsi_14"), 1e-9)


def _hlc(ref: pl.DataFrame) -> pl.DataFrame:
    return float64_frame({c: as_floats(ref, c) for c in ("high", "low", "close")})


def test_true_range_matches_stockcharts_sheets() -> None:
    for name in ("stockcharts_atr_14.csv", "stockcharts_adx_14.csv"):
        ref = read_reference(name)
        program = Program()
        tr = recursive.true_range(program).alias("tr")
        got = program.apply(_hlc(ref).lazy()).select(tr).collect()["tr"].to_list()
        expected = as_floats(ref, "tr")
        # StockCharts publica TR_0 = H - L en la hoja de ATR; RF-2 define TR desde t=1.
        assert got[0] is None
        _assert_close(got[1:], expected[1:], 1e-9)


def test_atr_matches_stockcharts_sheet_with_documented_seed_adjustment() -> None:
    """Semilla RF-2 (media de TR[1..14] en t=14) y recursión de Wilder idéntica a StockCharts."""
    ref = read_reference("stockcharts_atr_14.csv")
    out = apply_kernel(recursive.atr, {"period": 14}, _hlc(ref))["value"].to_list()
    tr = as_floats(ref, "tr")
    published = as_floats(ref, "atr_stockcharts")
    assert all(v is None for v in out[:14])
    seed = sum(v for v in tr[1:15] if v is not None) / 14
    assert out[14] == pytest.approx(seed, rel=1e-12)
    # Misma recursión de Wilder ⇒ la diferencia de semilla decae exactamente como (13/14)^k.
    o14, p14 = out[14], published[14]
    assert o14 is not None and p14 is not None
    diff0 = o14 - p14
    for t in range(15, len(out)):
        pt = published[t]
        assert pt is not None
        assert out[t] == pytest.approx(pt + diff0 * (13 / 14) ** (t - 14), rel=1e-9), t


def test_adx_matches_stockcharts_sheet_from_stable_from() -> None:
    """Ajuste de convención documentado: semilla TA-Lib (Σ de 13 + 1 paso) vs StockCharts (Σ de 14).

    La diferencia de semilla decae como (13/14)^k: desde ``stable_from`` (±DI 200, ADX 387)
    coinciden con rtol 1e-6; el calentamiento (primera fila no nula) es el mismo.
    """
    ref = read_reference("stockcharts_adx_14.csv")
    out = apply_kernel(recursive.adx, {"period": 14}, _hlc(ref))
    for col, ref_col, start in (
        ("plus_di", "plus_di", 200),
        ("minus_di", "minus_di", 200),
        ("value", "adx", 387),
    ):
        got, expected = out[col].to_list(), as_floats(ref, ref_col)
        first = next(i for i, v in enumerate(expected) if v is not None)
        assert all(v is None for v in got[:first]) and got[first] is not None, col
        for t in range(start, len(expected)):
            e, g = expected[t], got[t]
            assert e is not None and g is not None
            assert math.isclose(g, e, rel_tol=1e-6), (col, t, g, e)


def test_di_follows_talib_seed_convention() -> None:
    """S_period = Σ(x_1..x_{period-1})·(1 − 1/period) + x_period (TA-Lib), no Σ(x_1..x_period)."""
    n = 3
    frame = ohlcv_frame(
        {
            "high": [10.0, 12.0, 11.0, 15.0, 14.0],
            "low": [9.0, 10.0, 10.0, 12.0, 13.0],
            "close": [9.5, 11.0, 10.5, 14.0, 13.5],
        }
    )
    out = apply_kernel(recursive.adx, {"period": n}, frame)
    tr1, tr2, tr3 = 2.5, 1.0, 4.5  # max(H-L, |H-Cp|, |L-Cp|) en t = 1, 2, 3
    pdm1, pdm2, pdm3 = 2.0, 0.0, 4.0  # +DM en t = 1, 2, 3
    s_tr = (tr1 + tr2) * (1 - 1 / n) + tr3
    s_p = (pdm1 + pdm2) * (1 - 1 / n) + pdm3
    plus_di = out["plus_di"].to_list()
    assert plus_di[:3] == [None, None, None]
    assert plus_di[3] == pytest.approx(100 * s_p / s_tr, rel=1e-12)


def test_macd_matches_hand_calculation() -> None:
    ref = read_reference("hand_macd_2_3_2.csv")
    frame = float64_frame({"close": as_floats(ref, "close")})
    params: Params = {"fast": 2, "slow": 3, "signal": 2, "source": "close"}
    out = apply_kernel(recursive.macd, params, frame)
    for suffix in ("line", "signal", "hist"):
        _assert_close(out[suffix].to_list(), as_floats(ref, f"macd_2_3_2_{suffix}"), 1e-12)


# ------------------------------------------------------------------------------ bordes

FIRST_NON_NULL: list[tuple[KernelFn, Params, dict[str, int]]] = [
    (recursive.ema, {"period": 20, "source": "close"}, {"value": 19}),
    (recursive.rsi, {"period": 14, "source": "close"}, {"value": 14}),
    (recursive.atr, {"period": 14}, {"value": 14}),
    (recursive.adx, {"period": 14}, {"value": 27, "plus_di": 14, "minus_di": 14}),
    (
        recursive.macd,
        {"fast": 12, "slow": 26, "signal": 9, "source": "close"},
        {"line": 25, "signal": 33, "hist": 33},
    ),
]


@pytest.mark.parametrize(("kernel", "params", "first"), FIRST_NON_NULL)
def test_first_non_null_row_is_warmup_index(
    kernel: KernelFn, params: Params, first: dict[str, int]
) -> None:
    out = apply_kernel(kernel, params, ohlcv_frame(random_walk(120)))
    assert set(out.columns) == set(first)
    for col, idx in first.items():
        values = out[col].to_list()
        assert all(v is None for v in values[:idx]), col
        assert all(v is not None for v in values[idx:]), col
    assert all(dtype == pl.Float64 for dtype in out.schema.values())


def test_flat_series_rsi_and_adx_are_zero() -> None:
    flat = ohlcv_frame({c: [50.0] * 60 for c in ("high", "low", "close")})
    rsi = apply_kernel(recursive.rsi, {"period": 14, "source": "close"}, flat)["value"]
    assert rsi.drop_nulls().to_list() == [0.0] * (60 - 14)
    adx = apply_kernel(recursive.adx, {"period": 14}, flat)
    for col, first in (("value", 27), ("plus_di", 14), ("minus_di", 14)):
        assert adx[col].drop_nulls().to_list() == [0.0] * (60 - first), col


@pytest.mark.parametrize(("kernel", "params", "first"), FIRST_NON_NULL)
def test_null_rows_are_excluded_from_recursion(
    kernel: KernelFn, params: Params, first: dict[str, int]
) -> None:
    """Fila nula ⇒ salida nula y estado intacto: igual que eliminar la fila (bit a bit)."""
    base = random_walk(150, seed=5)
    holes = {3, 40, 41, 77, 120}
    with_nulls = {c: list(v) for c, v in base.items()}
    for i in holes:
        for c in ("high", "low", "close"):
            with_nulls[c][i] = None if i % 2 else math.nan
    compact = {c: [v for i, v in enumerate(vals) if i not in holes] for c, vals in base.items()}
    got = apply_kernel(kernel, params, ohlcv_frame(with_nulls))
    ref = apply_kernel(kernel, params, ohlcv_frame(compact))
    kept = [i for i in range(150) if i not in holes]
    for col in first:
        values = got[col].to_list()
        assert all(values[i] is None for i in holes), col
        assert [values[i] for i in kept] == ref[col].to_list(), col
