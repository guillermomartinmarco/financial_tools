"""T2.1: catálogo (``ParamDef``, ``IndicatorDef``, ``CATALOG``, ``resolve``, ``catalog()``)."""

from __future__ import annotations

import pytest
from finances_core.indicators_store.namespaces import belongs_to
from finances_core.schemas.indicators import IndicatorCatalogEntry, IndicatorSpec

from finances_analytics.ft_indicators.catalog import CATALOG, catalog, resolve
from finances_analytics.ft_indicators.errors import IndicatorValidationError

RF2_NAMES = ["sma", "ema", "wma", "rsi", "macd", "bb", "atr", "stoch", "roc", "obv", "adx", "vwap"]

DEFAULTS = {
    "sma": (["sma_20"], ("close",), {"sma_20": 19}),
    "ema": (["ema_20"], ("close",), {"ema_20": 19}),
    "wma": (["wma_20"], ("close",), {"wma_20": 19}),
    "rsi": (["rsi_14"], ("close",), {"rsi_14": 14}),
    "macd": (
        ["macd_12_26_9_line", "macd_12_26_9_signal", "macd_12_26_9_hist"],
        ("close",),
        {"macd_12_26_9_line": 25, "macd_12_26_9_signal": 33, "macd_12_26_9_hist": 33},
    ),
    "bb": (
        ["bb_20_2_middle", "bb_20_2_upper", "bb_20_2_lower"],
        ("close",),
        {"bb_20_2_middle": 19, "bb_20_2_upper": 19, "bb_20_2_lower": 19},
    ),
    "atr": (["atr_14"], ("high", "low", "close"), {"atr_14": 14}),
    "stoch": (
        ["stoch_14_3_3_k", "stoch_14_3_3_d"],
        ("high", "low", "close"),
        {"stoch_14_3_3_k": 15, "stoch_14_3_3_d": 17},
    ),
    "roc": (["roc_12"], ("close",), {"roc_12": 12}),
    "obv": (["obv"], ("close", "volume"), {"obv": 0}),
    "adx": (
        ["adx_14", "adx_14_plus_di", "adx_14_minus_di"],
        ("high", "low", "close"),
        {"adx_14": 27, "adx_14_plus_di": 14, "adx_14_minus_di": 14},
    ),
    "vwap": (["vwap_20"], ("high", "low", "close", "volume"), {"vwap_20": 19}),
}


def test_catalog_has_exactly_the_rf2_entries() -> None:
    assert list(CATALOG) == RF2_NAMES


@pytest.mark.parametrize("name", RF2_NAMES)
def test_defaults_resolve_to_rf2_columns_inputs_and_warmup(name: str) -> None:
    columns, inputs, warmups = DEFAULTS[name]
    resolved = resolve(IndicatorSpec.model_validate({"name": name}))
    assert list(resolved.columns.values()) == columns
    assert resolved.inputs == inputs
    assert resolved.warmups() == warmups
    assert belongs_to("ft_indicators", resolved.indicator_id)
    assert not resolved.indicator_id.startswith("talib_")


def test_default_catalog_has_19_columns() -> None:
    total = sum(len(resolve(IndicatorSpec.model_validate({"name": n})).columns) for n in RF2_NAMES)
    assert total == 19


def test_resolved_spec_contains_all_params_in_catalog_order() -> None:
    resolved = resolve(IndicatorSpec(name="macd", params={"signal": 5}))
    assert list(resolved.params) == ["fast", "slow", "signal", "source"]
    assert resolved.spec() == IndicatorSpec(
        name="macd", params={"fast": 12, "slow": 26, "signal": 5, "source": "close"}
    )
    assert resolved.indicator_id == "macd_12_26_5"


@pytest.mark.parametrize(
    ("spec", "columns", "expected_id"),
    [
        ({"name": "bb", "params": {"std": 2.5}}, None, "bb_20_2p5"),
        ({"name": "bb", "params": {"std": 2}}, None, "bb_20_2"),
        ({"name": "bb", "params": {"std": 2.0}}, None, "bb_20_2"),
        (
            {"name": "sma", "params": {"period": 50, "source": "adj_close"}},
            {"close", "adj_close"},
            "sma_50_adj_close",
        ),
        ({"name": "roc", "params": {"period": 1}}, None, "roc_1"),
        ({"name": "rsi", "params": {"period": 14.0}}, None, "rsi_14"),
        ({"name": "ema", "params": {"source": "close"}}, None, "ema_20"),
        ({"name": "ema", "params": {"source": "open"}}, None, "ema_20_open"),
    ],
)
def test_resolve_ids(spec: dict[str, object], columns: set[str] | None, expected_id: str) -> None:
    resolved = resolve(IndicatorSpec.model_validate(spec), columns)
    assert resolved.indicator_id == expected_id


@pytest.mark.parametrize(
    ("spec", "columns", "fragment"),
    [
        ({"name": "sma", "params": {"period": 1}}, None, "[2, 10000]"),
        ({"name": "sma", "params": {"period": 10001}}, None, "[2, 10000]"),
        ({"name": "roc", "params": {"period": 0}}, None, "[1, 10000]"),
        ({"name": "rsi", "params": {"period": 14.5}}, None, "entero"),
        ({"name": "rsi", "params": {"period": "x"}}, None, "entero"),
        ({"name": "rsi", "params": {"window": 14}}, None, "window"),
        ({"name": "macd", "params": {"fast": 26, "slow": 12}}, None, "fast < slow"),
        ({"name": "macd", "params": {"fast": 12, "slow": 12}}, None, "fast < slow"),
        ({"name": "bb", "params": {"std": 2.555}}, None, "2 decimales"),
        ({"name": "bb", "params": {"std": 0}}, None, "(0, 10]"),
        ({"name": "bb", "params": {"std": 10.01}}, None, "(0, 10]"),
        ({"name": "bb", "params": {"std": "two"}}, None, "numérico"),
        ({"name": "atr", "params": {"source": "close"}}, None, "source"),
        ({"name": "vwap", "params": {"source": "close"}}, None, "source"),
        ({"name": "sma", "params": {"source": "volume"}}, None, "adj_close"),
        ({"name": "sma", "params": {"source": "adj_close"}}, {"close"}, "adj_close"),
        ({"name": "obv", "params": {"period": 3}}, None, "period"),
    ],
)
def test_invalid_params_are_rejected_with_detail(
    spec: dict[str, object], columns: set[str] | None, fragment: str
) -> None:
    with pytest.raises(IndicatorValidationError) as exc:
        resolve(IndicatorSpec.model_validate(spec), columns)
    assert fragment in str(exc.value)
    assert exc.value.indicator == spec["name"]


def test_validation_error_names_param_value_and_range() -> None:
    with pytest.raises(IndicatorValidationError) as exc:
        resolve(IndicatorSpec(name="sma", params={"period": 1}))
    err = exc.value
    assert (err.param, err.value) == ("period", 1)
    assert "[2, 10000]" in err.valid


def test_bool_param_is_rejected() -> None:
    with pytest.raises(IndicatorValidationError):
        resolve(IndicatorSpec(name="rsi", params={"period": True}))


def test_catalog_entries_are_public_contract() -> None:
    entries = catalog()
    assert [e.name for e in entries] == RF2_NAMES
    assert all(isinstance(e, IndicatorCatalogEntry) for e in entries)
    by_name = {e.name: e for e in entries}
    assert by_name["vwap"].talib_equivalent is None
    assert by_name["rsi"].params["period"] == {"type": "int", "default": 14, "min": 2, "max": 10000}
    assert by_name["roc"].params["period"]["min"] == 1
    assert by_name["bb"].params["source"]["choices"] == [
        "open",
        "high",
        "low",
        "close",
        "adj_close",
    ]
    assert by_name["adx"].outputs == ["", "plus_di", "minus_di"]
    assert by_name["sma"].outputs == []
    assert by_name["macd"].warmup
    assert all(e.engine_version >= 1 and e.convention for e in entries)
