"""T2.4: kernels de memoria finita (``sma``, ``wma``, ``bb``, ``roc``, ``vwap``, ``stoch``)."""

from __future__ import annotations

import math
import statistics

import polars as pl
import pytest

from finances_analytics.ft_indicators.kernels import windowed
from finances_analytics.ft_indicators.kernels.common import Params, Program

from .helpers import KernelFn, apply_kernel, as_floats, float64_frame, ohlcv_frame, read_reference


def _close(values: list[float | None]) -> pl.DataFrame:
    return float64_frame({"close": values})


def _assert_close(got: list[float | None], expected: list[float | None], rtol: float) -> None:
    assert len(got) == len(expected)
    for i, (g, e) in enumerate(zip(got, expected, strict=True)):
        if e is None:
            assert g is None, f"fila {i}: se esperaba nulo, hay {g}"
        else:
            assert g is not None, f"fila {i}: nulo inesperado (se esperaba {e})"
            assert math.isclose(g, e, rel_tol=rtol, abs_tol=0.0), f"fila {i}: {g} != {e}"


# ------------------------------------------------------------------ fixtures publicados


def test_sma_matches_stockcharts_sheet() -> None:
    ref = read_reference("stockcharts_sma_ema_10.csv")
    out = apply_kernel(
        windowed.sma, {"period": 10, "source": "close"}, _close(as_floats(ref, "close"))
    )
    _assert_close(out["value"].to_list(), as_floats(ref, "sma_10"), 1e-9)


def test_bb_matches_stockcharts_sheet_population_sigma() -> None:
    ref = read_reference("stockcharts_bb_20_2.csv")
    params = {"period": 20, "std": 2.0, "source": "close"}
    out = apply_kernel(windowed.bb, params, _close(as_floats(ref, "close")))
    for suffix in ("middle", "upper", "lower"):
        _assert_close(out[suffix].to_list(), as_floats(ref, f"bb_20_2_{suffix}"), 1e-9)


def test_roc_matches_stockcharts_sheet() -> None:
    ref = read_reference("stockcharts_roc_12.csv")
    out = apply_kernel(
        windowed.roc, {"period": 12, "source": "close"}, _close(as_floats(ref, "close"))
    )
    _assert_close(out["value"].to_list(), as_floats(ref, "roc_12"), 1e-9)


def _stoch_reference_frame() -> tuple[pl.DataFrame, list[float | None]]:
    ref = read_reference("stockcharts_stoch_14.csv")
    highs, lows = as_floats(ref, "high"), as_floats(ref, "low")
    closes = as_floats(ref, "close")
    # La hoja no publica el cierre de las filas 0..12 (el %K rápido solo usa C_t): se rellena con
    # el mínimo de la vela: no afecta a ninguna salida (las ventanas exigen filas no nulas).
    filled = [c if c is not None else lo for c, lo in zip(closes, lows, strict=True)]
    frame = float64_frame({"high": highs, "low": lows, "close": filled})
    return frame, as_floats(ref, "fast_k_14")


def test_stoch_fast_k_matches_stockcharts_sheet() -> None:
    frame, fast_k = _stoch_reference_frame()
    program = Program()
    fast = windowed.stoch_fast_k(program, 14).alias("fast_k")
    got = program.apply(frame.lazy()).select(fast).collect()["fast_k"].to_list()
    _assert_close(got, fast_k, 1e-9)


def test_stoch_full_k_and_d_are_smas_of_published_fast_k() -> None:
    frame, fast_k = _stoch_reference_frame()
    out = apply_kernel(windowed.stoch, {"k": 14, "smooth_k": 3, "d": 3}, frame)
    exp_k: list[float | None] = [None] * len(fast_k)
    for t in range(15, len(fast_k)):
        exp_k[t] = statistics.fmean(v for v in fast_k[t - 2 : t + 1] if v is not None)
    exp_d: list[float | None] = [None] * len(fast_k)
    for t in range(17, len(fast_k)):
        exp_d[t] = statistics.fmean(v for v in exp_k[t - 2 : t + 1] if v is not None)
    _assert_close(out["k"].to_list(), exp_k, 1e-9)
    _assert_close(out["d"].to_list(), exp_d, 1e-9)


def test_wma_matches_hand_calculation() -> None:
    ref = read_reference("hand_wma_3.csv")
    out = apply_kernel(
        windowed.wma, {"period": 3, "source": "close"}, _close(as_floats(ref, "close"))
    )
    _assert_close(out["value"].to_list(), as_floats(ref, "wma_3"), 1e-12)


def test_vwap_matches_hand_calculation() -> None:
    ref = read_reference("hand_vwap_3.csv")
    cols = {c: as_floats(ref, c) for c in ("high", "low", "close", "volume")}
    out = apply_kernel(windowed.vwap, {"period": 3}, float64_frame(cols))
    _assert_close(out["value"].to_list(), as_floats(ref, "vwap_3"), 1e-12)


# ------------------------------------------------------------------------------ bordes

WARMUP_CASES: list[tuple[KernelFn, Params, dict[str, int]]] = [
    (windowed.sma, {"period": 5, "source": "close"}, {"value": 4}),
    (windowed.wma, {"period": 5, "source": "close"}, {"value": 4}),
    (
        windowed.bb,
        {"period": 5, "std": 2.0, "source": "close"},
        {"middle": 4, "upper": 4, "lower": 4},
    ),
    (windowed.roc, {"period": 5, "source": "close"}, {"value": 5}),
    (windowed.vwap, {"period": 5}, {"value": 4}),
    (windowed.stoch, {"k": 5, "smooth_k": 3, "d": 4}, {"k": 6, "d": 9}),
]


@pytest.mark.parametrize(("kernel", "params", "warmups"), WARMUP_CASES)
def test_warmup_is_exact(kernel: KernelFn, params: Params, warmups: dict[str, int]) -> None:
    n = 40
    frame = ohlcv_frame(
        {
            "high": [101.0 + (i % 7) for i in range(n)],
            "low": [99.0 - (i % 5) for i in range(n)],
            "close": [100.0 + (i % 3) for i in range(n)],
            "volume": [10.0 + i for i in range(n)],
        }
    )
    out = apply_kernel(kernel, params, frame)
    for col, warm in warmups.items():
        values = out[col].to_list()
        assert all(v is None for v in values[:warm]), col
        assert all(v is not None for v in values[warm:]), col


@pytest.mark.parametrize(("kernel", "params", "warmups"), WARMUP_CASES)
def test_window_with_null_row_is_null(
    kernel: KernelFn, params: Params, warmups: dict[str, int]
) -> None:
    n, hole = 40, 20
    columns: dict[str, list[float | None]] = {
        "high": [101.0 + (i % 7) for i in range(n)],
        "low": [99.0 - (i % 5) for i in range(n)],
        "close": [100.0 + (i % 3) for i in range(n)],
        "volume": [10.0 + i for i in range(n)],
    }
    columns["close"][hole] = None
    columns["high"][hole] = math.nan  # NaN = nulo (RF-3)
    out = apply_kernel(kernel, params, ohlcv_frame(columns))
    for col, warm in warmups.items():
        values = out[col].to_list()
        # La fila nula anula justo las ventanas que la contienen (memoria = calentamiento + 1).
        assert all(v is None for v in values[hole : hole + warm + 1]), col
        assert values[hole - 1] is not None, col
        assert values[hole + warm + 1] is not None, col


def test_roc_base_zero_is_null() -> None:
    out = apply_kernel(
        windowed.roc, {"period": 2, "source": "close"}, _close([0.0, 1.0, 2.0, 3.0, 4.0])
    )
    assert out["value"].to_list() == [None, None, None, 200.0, 100.0]


def test_vwap_zero_volume_window_is_null() -> None:
    frame = float64_frame(
        {
            "high": [10.0] * 6,
            "low": [10.0] * 6,
            "close": [10.0] * 6,
            "volume": [1e9, 0.0, 0.0, 0.0, 0.0, 5.0],
        }
    )
    out = apply_kernel(windowed.vwap, {"period": 3}, frame)
    assert out["value"].to_list() == [None, None, 10.0, None, None, 10.0]


def test_stoch_hh_equals_ll_is_zero() -> None:
    frame = float64_frame({"high": [5.0] * 8, "low": [5.0] * 8, "close": [5.0] * 8})
    out = apply_kernel(windowed.stoch, {"k": 3, "smooth_k": 2, "d": 2}, frame)
    assert out["k"].to_list() == [None, None, None, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert out["d"].to_list() == [None, None, None, None, 0.0, 0.0, 0.0, 0.0]


def test_bb_uses_population_sigma() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    out = apply_kernel(windowed.bb, {"period": 4, "std": 1.0, "source": "close"}, _close(values))
    sigma = statistics.pstdev(values)
    assert out["upper"][3] == pytest.approx(2.5 + sigma, rel=1e-15)
    assert out["lower"][3] == pytest.approx(2.5 - sigma, rel=1e-15)
    assert sigma != statistics.stdev(values)


def test_source_parameter_selects_input_column() -> None:
    frame = float64_frame({"close": [1.0, 2.0, 3.0], "adj_close": [10.0, 20.0, 30.0]})
    out = apply_kernel(windowed.sma, {"period": 2, "source": "adj_close"}, frame)
    assert out["value"].to_list() == [None, 15.0, 25.0]


@pytest.mark.parametrize(("kernel", "params", "warmups"), WARMUP_CASES)
def test_kernel_outputs_are_float64(
    kernel: KernelFn, params: Params, warmups: dict[str, int]
) -> None:
    frame = ohlcv_frame(
        {c: [1.0 + i for i in range(30)] for c in ("high", "low", "close", "volume")}
    )
    out = apply_kernel(kernel, params, frame)
    assert set(out.columns) == set(warmups)
    assert all(dtype == pl.Float64 for dtype in out.schema.values())
