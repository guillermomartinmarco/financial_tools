"""T3.2: motor — recálculo completo, alineación 1:1, determinismo y frontera Float64→Float32."""

from __future__ import annotations

import ast
import math
from pathlib import Path

import polars as pl
import pytest
from finances_core.schemas.indicators import IndicatorSpec
from polars.testing import assert_frame_equal

from finances_analytics.ft_indicators import engine
from finances_analytics.ft_indicators.catalog import CATALOG, ResolvedIndicator, resolve

from .helpers import ohlcv_frame, random_walk

SRC = Path(__file__).resolve().parents[2] / "src" / "finances_analytics" / "ft_indicators"


def _all_defaults() -> list[ResolvedIndicator]:
    return [resolve(IndicatorSpec.model_validate({"name": n})) for n in CATALOG]


def _frame(n: int = 400) -> pl.DataFrame:
    return ohlcv_frame(random_walk(n, seed=9))


# ------------------------------------------------------------- RFC-0001: frontera de tipos


def _modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _float64_allowed(path: Path) -> bool:
    rel = path.relative_to(SRC).as_posix()
    return rel == "engine.py" or rel.startswith("kernels/")


def test_float64_only_inside_kernels_and_engine() -> None:
    """RFC-0001: ningún módulo fuera de ``kernels/`` y ``engine.py`` nombra ``Float64``."""
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        if "Float64" in names and not _float64_allowed(path):
            offenders.append(path.name)
    assert offenders == []


def _float32_casts(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "cast"
            and node.args
        ):
            arg = node.args[0]
            if (isinstance(arg, ast.Attribute) and arg.attr == "Float32") or (
                isinstance(arg, ast.Name) and arg.id == "PERSIST_DTYPE"
            ):
                count += 1
    return count


def test_single_float32_cast_point_in_engine() -> None:
    """RFC-0001: un único ``cast`` a ``Float32`` en todo ``ft_indicators`` y está en el motor."""
    casts = {p.name: _float32_casts(ast.parse(p.read_text(encoding="utf-8"))) for p in _modules()}
    assert casts["engine.py"] == 1
    assert sum(casts.values()) == 1
    assert pl.Float32 == engine.PERSIST_DTYPE


def test_kernels_compute_float64_and_persisted_is_float32() -> None:
    frame, indicators = _frame(), _all_defaults()
    f64 = engine.compute_float64(frame, indicators)
    result = engine.compute(frame, indicators)
    columns = [c for r in indicators for c in r.columns.values()]
    assert len(columns) == 19
    assert f64.columns == ["open_time_ms", *columns]
    assert result.frame.columns == ["open_time_ms", *columns]
    assert all(f64.schema[c] == pl.Float64 for c in columns)
    assert all(result.frame.schema[c] == pl.Float32 for c in columns)
    assert result.frame.schema["open_time_ms"] == pl.Int64
    # El cast es la única diferencia entre oráculo y persistido.
    assert_frame_equal(f64.with_columns(pl.col(columns).cast(pl.Float32)), result.frame)


def test_alignment_one_to_one_and_warmup_kept_as_null() -> None:
    frame = _frame()
    result = engine.compute(frame, _all_defaults())
    assert result.frame.height == frame.height
    assert result.frame["open_time_ms"].to_list() == frame["open_time_ms"].to_list()
    for resolved in _all_defaults():
        for column, warm in resolved.warmups().items():
            nulls = result.frame[column].is_null().to_list()
            assert nulls == [True] * warm + [False] * (frame.height - warm), column


def test_deterministic_full_recompute() -> None:
    frame = _frame()
    first = engine.compute(frame, _all_defaults())
    second = engine.compute(frame.clone(), _all_defaults())
    assert_frame_equal(first.frame, second.frame, check_exact=True)
    assert first.outputs.keys() == second.outputs.keys()
    for key, out in first.outputs.items():
        assert out.stability == second.outputs[key].stability
        assert out.warnings == second.outputs[key].warnings


def test_series_frame_and_stability_per_indicator() -> None:
    frame = _frame(500)
    result = engine.compute(frame, _all_defaults())
    times = frame["open_time_ms"].to_list()
    assert result.outputs["ema_20"].stability.stable_from_ms == times[158]
    assert result.outputs["adx_14"].stability.stable_from_ms == times[387]
    assert result.outputs["sma_20"].stability.stable_from_ms == times[19]
    assert result.outputs["obv"].stability.reason == "non_decaying_seed"
    rsi = result.series_frame("rsi_14")
    assert rsi.columns == ["open_time_ms", "rsi_14"]
    assert rsi.schema["rsi_14"] == pl.Float32


def test_duplicate_indicators_are_computed_once() -> None:
    rsi = resolve(IndicatorSpec(name="rsi"))
    result = engine.compute(
        _frame(50), [rsi, resolve(IndicatorSpec(name="rsi", params={"period": 14}))]
    )
    assert list(result.outputs) == ["rsi_14"]
    assert result.frame.columns == ["open_time_ms", "rsi_14"]


def test_insufficient_history_column_is_all_null_with_warning() -> None:
    frame = _frame(10)
    result = engine.compute(
        frame, [resolve(IndicatorSpec(name="sma")), resolve(IndicatorSpec(name="obv"))]
    )
    assert result.frame["sma_20"].null_count() == 10
    assert "insufficient_history" in result.outputs["sma_20"].warnings
    assert "insufficient_history" not in result.outputs["obv"].warnings
    assert result.outputs["sma_20"].stability.stable_from_ms is None


def test_not_converged_warning_when_series_is_shorter_than_stable_index() -> None:
    result = engine.compute(_frame(100), [resolve(IndicatorSpec(name="ema"))])
    out = result.outputs["ema_20"]
    assert "not_converged" in out.warnings
    assert "insufficient_history" not in out.warnings
    assert out.stability.stable_from_ms is None


def test_null_inputs_are_counted_in_warnings() -> None:
    data = random_walk(60, seed=2)
    data["close"][5] = None
    data["close"][9] = math.nan
    data["high"][9] = math.inf
    data["high"][20] = None
    result = engine.compute(
        ohlcv_frame(data), [resolve(IndicatorSpec(name="ema")), resolve(IndicatorSpec(name="atr"))]
    )
    assert "null_inputs=2" in result.outputs["ema_20"].warnings
    assert "null_inputs=3" in result.outputs["atr_14"].warnings
    assert result.frame["ema_20"][5] is None


def test_missing_input_column_is_rejected() -> None:
    frame = _frame(30).drop("volume")
    with pytest.raises(ValueError, match="volume"):
        engine.compute(frame, [resolve(IndicatorSpec(name="obv"))])


def test_empty_source_produces_empty_aligned_output() -> None:
    result = engine.compute(_frame(10).head(0), _all_defaults())
    assert result.frame.height == 0
    assert all(o.stability.stable_from_ms is None for o in result.outputs.values())
