"""T2.6: kernel acumulado ``obv``."""

from __future__ import annotations

import math

from finances_analytics.ft_indicators.kernels import cumulative

from .helpers import apply_kernel, as_floats, float64_frame, ohlcv_frame, read_reference


def test_obv_matches_hand_calculation() -> None:
    ref = read_reference("hand_obv.csv")
    frame = float64_frame({"close": as_floats(ref, "close"), "volume": as_floats(ref, "volume")})
    out = apply_kernel(cumulative.obv, {}, frame)
    assert out["value"].to_list() == as_floats(ref, "obv")


def test_obv_first_row_is_first_volume() -> None:
    out = apply_kernel(cumulative.obv, {}, ohlcv_frame({"close": [7.0], "volume": [42.0]}))
    assert out["value"].to_list() == [42.0]


def test_obv_up_down_equal() -> None:
    frame = ohlcv_frame({"close": [1.0, 2.0, 1.0, 1.0, 3.0], "volume": [1.0, 2.0, 4.0, 8.0, 16.0]})
    out = apply_kernel(cumulative.obv, {}, frame)
    assert out["value"].to_list() == [1.0, 3.0, -1.0, -1.0, 15.0]


def test_obv_null_row_is_null_and_keeps_accumulator() -> None:
    frame = ohlcv_frame(
        {
            "close": [1.0, 2.0, None, 3.0, math.nan, 2.0],
            "volume": [1.0, 2.0, 4.0, 8.0, 16.0, math.inf],
        }
    )
    out = apply_kernel(cumulative.obv, {}, frame)
    # Fila 3 compara con el último cierre válido (fila 1); la fila 5 tiene volumen no finito.
    assert out["value"].to_list() == [1.0, 3.0, None, 11.0, None, None]


def test_obv_leading_nulls_start_at_first_valid_row() -> None:
    frame = ohlcv_frame({"close": [None, 5.0, 6.0], "volume": [3.0, 10.0, 1.0]})
    out = apply_kernel(cumulative.obv, {}, frame)
    assert out["value"].to_list() == [None, 10.0, 11.0]


def test_obv_is_float64() -> None:
    out = apply_kernel(cumulative.obv, {}, ohlcv_frame({"close": [1.0, 2.0], "volume": [1.0, 1.0]}))
    assert str(out.schema["value"]) == "Float64"
