"""T2.3: ``resolver.resolve`` + ``resolver.probe`` (RF-3, RF-4, RF-9; plan §3.2)."""

from __future__ import annotations

import pytest
from finances_core.indicators_store.namespaces import belongs_to
from finances_core.schemas.ta_lib import MA_TYPE_NAMES, TaLibSpec
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from finances_analytics.ft_ta_lib import catalog, engine, resolver
from finances_analytics.ft_ta_lib.errors import TaLibExecutionError, TaLibValidationError
from finances_analytics.ft_ta_lib.model import ResolvedFunction

OHLCV = frozenset({"open_time_ms", "open", "high", "low", "close", "volume"})
OHLCV_ADJ = OHLCV | {"adj_close"}


def _spec(function: str, **kwargs: object) -> TaLibSpec:
    return TaLibSpec.model_validate({"function": function, **kwargs})


def _resolve(function: str, **kwargs: object) -> ResolvedFunction:
    return resolver.resolve(_spec(function, **kwargs))


def test_rsi_defaults() -> None:
    rsi = _resolve("rsi")
    assert rsi.series_id == "talib_rsi_14"
    assert rsi.columns == {"real": "talib_rsi_14"}
    assert rsi.dtypes == {"talib_rsi_14": "Float32"}
    assert rsi.params == {"timeperiod": 14}
    assert rsi.inputs == {"price": ("close",)}
    assert rsi.lookback == 14
    assert rsi.spec() == _spec("RSI", params={"timeperiod": 14})


def test_rsi_price_override_and_missing_adj_close() -> None:
    spec = _spec("rsi", inputs={"price": "adj_close"})
    rsi = resolver.resolve(spec, OHLCV_ADJ)
    assert rsi.series_id == "talib_rsi_14_price_adj_close"
    assert rsi.inputs == {"price": ("adj_close",)}
    assert rsi.source_columns == ("adj_close",)
    with pytest.raises(TaLibValidationError, match="adj_close"):
        resolver.resolve(spec, OHLCV)


def test_override_equal_to_default_is_not_an_override() -> None:
    assert _resolve("rsi", inputs={"price": "close"}).series_id == "talib_rsi_14"


def test_bbands_matype_by_name_equals_by_integer() -> None:
    by_name = _resolve("bbands", params={"matype": "ema"})
    by_int = _resolve("bbands", params={"matype": 1})
    assert by_name == by_int
    assert by_name.params["matype"] == 1
    assert by_name.series_id == "talib_bbands_20_2_2_ema"
    assert list(by_name.columns.values()) == [
        "talib_bbands_20_2_2_ema_upperband",
        "talib_bbands_20_2_2_ema_middleband",
        "talib_bbands_20_2_2_ema_lowerband",
    ]
    assert by_name.spec().params["matype"] == "ema"


def test_float_param_accepts_integers() -> None:
    assert _resolve("bbands", params={"nbdevup": 3}).params["nbdevup"] == 3.0


def test_macd_three_outputs() -> None:
    macd = _resolve("macd")
    assert list(macd.columns.values()) == [
        "talib_macd_12_26_9_macd",
        "talib_macd_12_26_9_macdsignal",
        "talib_macd_12_26_9_macdhist",
    ]


@pytest.mark.parametrize(
    ("function", "series_id"),
    [
        ("cdlmorningstar", "talib_cdlmorningstar_0p3"),
        ("CDLENGULFING", "talib_cdlengulfing"),
        ("stoch", "talib_stoch_5_3_sma_3_sma"),
        ("minmaxindex", "talib_minmaxindex_30"),
        ("kdj", "talib_kdj_9_3_rma_3_rma"),
    ],
)
def test_spec_examples(function: str, series_id: str) -> None:
    assert _resolve(function).series_id == series_id


def test_index_and_pattern_dtypes() -> None:
    assert _resolve("minmaxindex").dtypes == {
        "talib_minmaxindex_30_minidx": "Int64",
        "talib_minmaxindex_30_maxidx": "Int64",
    }
    assert _resolve("cdlhikkake").dtypes == {"talib_cdlhikkake": "Int16"}


def test_unknown_function_lists_valid_groups() -> None:
    with pytest.raises(TaLibValidationError, match="Pattern Recognition") as info:
        _resolve("nope")
    assert info.value.function == "NOPE"


def test_unsupported_function_reports_reason() -> None:
    with pytest.raises(TaLibValidationError, match="unavailable_input"):
        _resolve("mavp")


@pytest.mark.parametrize(
    ("params", "pattern"),
    [
        ({"foo": 3}, "foo"),
        ({"timeperiod": 2.5}, "entero"),
        ({"timeperiod": "x"}, "entero"),
    ],
)
def test_invalid_params(params: dict[str, object], pattern: str) -> None:
    with pytest.raises(TaLibValidationError, match=pattern) as info:
        _resolve("rsi", params=params)
    assert info.value.param == next(iter(params))


@pytest.mark.parametrize(
    ("function", "params"),
    [
        ("rsi", {"timeperiod": 1}),
        ("rsi", {"timeperiod": 100_001}),
        ("cdlmorningstar", {"penetration": -1.0}),
        ("mama", {"fastlimit": 5.0}),
        ("t3", {"vfactor": 2.0}),
    ],
)
def test_ta_bad_param_reports_talib_message(function: str, params: dict[str, object]) -> None:
    with pytest.raises(TaLibValidationError, match="TA_BAD_PARAM"):
        _resolve(function, params=params)


@pytest.mark.parametrize("value", ["foo", 14, -1])
def test_invalid_matype(value: object) -> None:
    with pytest.raises(TaLibValidationError, match="matype"):
        _resolve("bbands", params={"matype": value})


def test_float_param_with_too_many_decimals_is_invalid() -> None:
    with pytest.raises(TaLibValidationError, match="decimales"):
        _resolve("bbands", params={"nbdevup": 2.1234567})


def test_redefining_composite_or_unknown_input_is_invalid() -> None:
    with pytest.raises(TaLibValidationError, match="compuesta"):
        _resolve("stoch", inputs={"prices": "close"})
    with pytest.raises(TaLibValidationError, match="price0"):
        _resolve("rsi", inputs={"price0": "high"})


def test_two_single_inputs_are_named_in_talib_order() -> None:
    beta = _resolve("beta", inputs={"price1": "close", "price0": "open"})
    assert beta.series_id == "talib_beta_5_price0_open_price1_close"
    assert beta.inputs == {"price0": ("open",), "price1": ("close",)}


def test_probe_accepts_valid_and_fixes_dtypes() -> None:
    fn = _resolve("cdlhikkake")
    assert resolver.probe(fn) is fn


def test_probe_turns_talib_errors_into_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_fn: ResolvedFunction, rows: int | None = None) -> list[bool]:
        raise TaLibExecutionError("RSI: TA_RSI function failed (TA_BAD_PARAM)")

    monkeypatch.setattr(engine, "probe", boom)
    with pytest.raises(TaLibValidationError, match="TA_BAD_PARAM"):
        resolver.probe(_resolve("rsi"))


def test_all_supported_functions_resolve_with_defaults() -> None:
    for name, meta in catalog.function_table().items():
        if not meta.supported:
            continue
        fn = resolver.probe(resolver.resolve(_spec(name)))
        assert belongs_to("ft_ta_lib", fn.series_id)
        assert all(belongs_to("ft_ta_lib", c) for c in fn.columns.values())
        assert fn.lookback == meta.default_lookback


# --- propiedad: specs distintas nunca colisionan -----------------------------------------

SUPPORTED = sorted(n for n, m in catalog.function_table().items() if m.supported)


@st.composite
def specs(draw: st.DrawFn) -> TaLibSpec:
    name = draw(st.sampled_from(SUPPORTED))
    meta = catalog.function_table()[name]
    params: dict[str, int | float | str] = {}
    for p in meta.params:
        if not draw(st.booleans()):
            continue
        if p.type == "int":
            params[p.name] = draw(st.integers(min_value=1, max_value=60))
        elif p.type == "matype":
            params[p.name] = draw(st.sampled_from(MA_TYPE_NAMES[:9]))
        else:
            params[p.name] = draw(st.sampled_from([0.0, 0.05, 0.3, 0.5, 1.0, 2.0, 2.5]))
    inputs: dict[str, str] = {}
    for i in meta.inputs:
        if i.single and draw(st.booleans()):
            inputs[i.name] = draw(st.sampled_from(["open", "high", "low", "close", "adj_close"]))
    return TaLibSpec.model_validate({"function": name, "params": params, "inputs": inputs})


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(specs(), min_size=2, max_size=6))
def test_distinct_specs_never_collide(drawn: list[TaLibSpec]) -> None:
    by_id: dict[str, TaLibSpec] = {}
    columns: dict[str, str] = {}
    for spec in drawn:
        try:
            fn = resolver.resolve(spec, OHLCV_ADJ)
        except TaLibValidationError:
            continue
        canonical = fn.spec()
        assert by_id.setdefault(fn.series_id, canonical) == canonical
        for col in fn.columns.values():
            assert columns.setdefault(col, fn.series_id) == fn.series_id


def test_default_series_ids_and_columns_are_unique_across_catalog() -> None:
    ids: list[str] = []
    cols: list[str] = []
    for name in SUPPORTED:
        fn = resolver.resolve(_spec(name))
        ids.append(fn.series_id)
        cols.extend(fn.columns.values())
    assert len(ids) == len(set(ids))
    assert len(cols) == len(set(cols))
