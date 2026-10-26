"""T3.1: ``engine.compute_function`` (RF-6, RF-7; plan §3.3; RFC-0002)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
import talib
from finances_core.schemas.ta_lib import TaLibSpec

from finances_analytics.ft_ta_lib import catalog, engine, resolver
from finances_analytics.ft_ta_lib.errors import OutputOutOfRangeError, TaLibExecutionError
from finances_analytics.ft_ta_lib.model import ResolvedFunction

from .conftest import ohlcv_frame

ENGINE_SRC = Path(engine.__file__).read_text(encoding="utf-8")
PATTERNS = catalog.groups()["Pattern Recognition"]


def _fn(function: str, **kwargs: object) -> ResolvedFunction:
    return resolver.resolve(TaLibSpec.model_validate({"function": function, **kwargs}))


def _f64(frame: pl.DataFrame, column: str) -> np.ndarray[Any, Any]:
    return frame[column].to_numpy().astype(np.float64)


def _as_f32_bits(values: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Float32 con NaN canónico (para comparar bit a bit nulos incluidos)."""
    out = values.astype(np.float32)
    out[~np.isfinite(out)] = np.nan
    return out


@pytest.fixture(scope="module")
def frame() -> pl.DataFrame:
    return ohlcv_frame(600, seed=11)


@pytest.mark.parametrize(
    ("function", "direct"),
    [
        ("RSI", lambda d: [talib.RSI(d["close"], 14)]),
        ("KAMA", lambda d: [talib.KAMA(d["close"], 30)]),
        ("BBANDS", lambda d: list(talib.BBANDS(d["close"], 20, 2.0, 2.0, 0))),
        ("ATR", lambda d: [talib.ATR(d["high"], d["low"], d["close"], 14)]),
        ("HT_DCPERIOD", lambda d: [talib.HT_DCPERIOD(d["close"])]),
    ],
)
def test_real_output_equals_direct_talib_cast_to_float32(
    frame: pl.DataFrame, function: str, direct: Any
) -> None:
    fn = _fn(function)
    result = engine.compute_function(frame, fn)
    arrays = {c: _f64(frame, c) for c in ("open", "high", "low", "close", "volume")}
    for column, expected in zip(fn.columns.values(), direct(arrays), strict=True):
        got = result.frame[column]
        assert got.dtype == pl.Float32
        got_np = got.to_numpy().astype(np.float32)  # nulos → NaN
        want = _as_f32_bits(expected)
        assert np.array_equal(np.isnan(got_np), np.isnan(want))
        mask = ~np.isnan(want)
        assert np.array_equal(got_np[mask].view(np.uint32), want[mask].view(np.uint32))
        assert got.is_nan().sum() == 0  # NaN de TA-Lib → nulo


def test_cdlhikkake_keeps_200_in_int16() -> None:
    big = ohlcv_frame(6000, seed=3)
    fn = _fn("CDLHIKKAKE")
    out = engine.compute_function(big, fn).frame["talib_cdlhikkake"]
    assert out.dtype == pl.Int16
    values = set(out.drop_nulls().unique().to_list())
    assert values <= {-200, -100, 0, 100, 200}
    assert values & {-200, 200}


@pytest.mark.parametrize("name", PATTERNS)
def test_pattern_warmup_rows_are_null(frame: pl.DataFrame, name: str) -> None:
    fn = _fn(name)
    out = engine.compute_function(frame, fn).frame[fn.series_id]
    assert out.dtype == pl.Int16
    assert out.head(fn.lookback).null_count() == fn.lookback
    assert out.slice(fn.lookback).null_count() == 0


def test_category_warmup_rows_are_null(frame: pl.DataFrame) -> None:
    fn = _fn("HT_TRENDMODE")
    out = engine.compute_function(frame, fn).frame["talib_ht_trendmode"]
    assert out.dtype == pl.Int16
    assert out.head(fn.lookback).null_count() == fn.lookback
    assert set(out.drop_nulls().unique().to_list()) <= {0, 1}


def _window_argmax_times(frame: pl.DataFrame, period: int, *, use_min: bool) -> list[int | None]:
    close = frame["close"].to_list()
    times = frame["open_time_ms"].to_list()
    out: list[int | None] = []
    for t in range(len(close)):
        if t < period - 1:
            out.append(None)
            continue
        window = close[t - period + 1 : t + 1]
        best = min(window) if use_min else max(window)
        out.append(times[t - period + 1 + window.index(best)])
    return out


def test_maxindex_and_minmaxindex_return_open_time_ms(frame: pl.DataFrame) -> None:
    fn = _fn("MAXINDEX")
    out = engine.compute_function(frame, fn).frame["talib_maxindex_30"]
    assert out.dtype == pl.Int64
    assert out.to_list() == _window_argmax_times(frame, 30, use_min=False)
    mm = _fn("MINMAXINDEX", params={"timeperiod": 10})
    res = engine.compute_function(frame, mm).frame
    assert res["talib_minmaxindex_10_minidx"].to_list() == _window_argmax_times(
        frame, 10, use_min=True
    )
    assert res["talib_minmaxindex_10_maxidx"].to_list() == _window_argmax_times(
        frame, 10, use_min=False
    )


def test_index_refers_to_source_rows_when_inputs_have_nulls(frame: pl.DataFrame) -> None:
    holed = frame.with_columns(
        pl.when(pl.int_range(pl.len()).is_in([40, 41, 100]))
        .then(None)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    fn = _fn("MAXINDEX", params={"timeperiod": 5})
    out = engine.compute_function(holed, fn).frame
    valid_times = set(holed.filter(pl.col("close").is_not_null())["open_time_ms"].to_list())
    refs = out["talib_maxindex_5"].drop_nulls().to_list()
    assert refs
    assert set(refs) <= valid_times
    # cada referencia es una fila anterior o igual
    for t, ref in zip(out["open_time_ms"], out["talib_maxindex_5"], strict=True):
        assert ref is None or ref <= t


def test_null_input_row_gives_null_output_and_no_nan(frame: pl.DataFrame) -> None:
    holed = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 50).then(None).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.int_range(pl.len()) == 70)
        .then(float("nan"))
        .otherwise(pl.col("high"))
        .cast(pl.Float32)
        .alias("high"),
    )
    sma = engine.compute_function(holed, _fn("SMA", params={"timeperiod": 5}))
    col = sma.frame["talib_sma_5"]
    assert sma.frame.height == holed.height
    assert col[50] is None
    assert col.slice(4).null_count() == 1  # solo la fila excluida
    assert col.is_nan().sum() == 0
    assert "null_inputs=1" in sma.warnings
    # la ventana "salta" la fila excluida: el valor en 51 es la media de 46..49 y 51
    closes = holed["close"].to_numpy().astype(np.float64)
    expected = np.float32(np.mean(closes[[46, 47, 48, 49, 51]]))
    assert col[51] == pytest.approx(float(expected), rel=1e-6)

    atr = engine.compute_function(holed, _fn("ATR"))
    assert atr.frame["talib_atr_14"][70] is None
    assert atr.frame["talib_atr_14"][50] is None
    assert atr.null_inputs == 2


def test_out_of_int16_output_raises_output_out_of_range(
    frame: pl.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    fn = _fn("CDLDOJI")

    def fake_run(_fn: ResolvedFunction, arrays: dict[str, np.ndarray[Any, Any]]) -> list[Any]:
        n = len(next(iter(arrays.values())))
        values = np.zeros(n, dtype=np.int32)
        values[-1] = 40_000
        return [values]

    monkeypatch.setattr(engine, "_run", fake_run)
    with pytest.raises(OutputOutOfRangeError, match="output_out_of_range") as info:
        engine.compute_function(frame, fn)
    assert info.value.code == "output_out_of_range"
    assert info.value.high == 40_000


def test_talib_exception_becomes_execution_error(
    frame: pl.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    fn = _fn("RSI")

    class Broken:
        def set_input_names(self, _names: object) -> None:
            return None

        def __call__(self, *_a: object, **_k: object) -> object:
            msg = "TA_RSI function failed with error code 2: Bad Parameter (TA_BAD_PARAM)"
            raise Exception(msg)  # noqa: TRY002 - imita la excepción genérica de TA-Lib

    monkeypatch.setattr(engine.talib.abstract, "Function", lambda _name: Broken())
    with pytest.raises(TaLibExecutionError, match="TA_BAD_PARAM"):
        engine.compute_function(frame, fn)


def test_missing_input_column_is_value_error(frame: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="adj_close"):
        engine.compute_function(
            frame,
            resolver.resolve(
                TaLibSpec.model_validate({"function": "RSI", "inputs": {"price": "adj_close"}})
            ),
        )


def test_adj_close_override_uses_that_column() -> None:
    frame = ohlcv_frame(200, adj_close=True)
    fn = resolver.resolve(
        TaLibSpec.model_validate({"function": "SMA", "inputs": {"price": "adj_close"}}),
        frame.columns,
    )
    out = engine.compute_function(frame, fn).frame[fn.series_id]
    expected = talib.SMA(_f64(frame, "adj_close"), 30).astype(np.float32)
    assert out[-1] == expected[-1]


def test_every_persisted_column_has_an_allowed_dtype(frame: pl.DataFrame) -> None:
    """RFC-0002: ``Float32`` (reales), ``Int16`` (patrón/categoría), ``Int64`` (tiempo, índice)."""
    for name, meta in catalog.function_table().items():
        if not meta.supported:
            continue
        fn = _fn(name)
        result = engine.compute_function(frame, fn).frame
        assert result.schema["open_time_ms"] == pl.Int64
        for out in meta.outputs:
            dtype = result.schema[fn.columns[out.name]]
            expected = {"real": pl.Float32, "pattern": pl.Int16, "category": pl.Int16}
            assert dtype == expected.get(out.kind, pl.Int64), (name, out)


# --- RFC-0002: un único punto de conversión desde float64 --------------------------------


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]


def _enclosing_functions(tree: ast.Module, predicate: Any) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(predicate(n) for n in ast.walk(node)):
            names.add(node.name)
    return names


def test_single_conversion_point_from_float64() -> None:
    tree = ast.parse(ENGINE_SRC)
    persist_casts = [
        c
        for c in _calls(tree, "cast")
        if c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == "PERSIST_REAL"
    ]
    assert len(persist_casts) == 1
    assert _enclosing_functions(tree, lambda n: n in persist_casts) == {"_persist_real"}
    float32_refs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in {"Float32", "float32"}
    ]
    assert len(float32_refs) == 1  # PERSIST_REAL = pl.Float32
    assert not _calls(tree, "astype")
    # float64 solo en la constante de cálculo y su uso en la frontera de entrada
    compute_users = _enclosing_functions(
        tree, lambda n: isinstance(n, ast.Name) and n.id == "COMPUTE_DTYPE"
    )
    assert compute_users == {"_as_compute_array", "synthetic_inputs"}
    f64_refs = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "float64"]
    assert len(f64_refs) == 1  # COMPUTE_DTYPE = np.float64


def test_global_config_is_read_only_snapshot() -> None:
    config = engine.global_config()
    assert config["compatibility"] == 0
    assert config["unstable_period.RSI"] == 0
    assert all(v == 0 for k, v in config.items() if k.startswith("unstable_period."))
