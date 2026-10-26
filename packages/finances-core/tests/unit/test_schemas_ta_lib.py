"""T1.1 (ft_ta_lib): contratos ``finances_core.schemas.ta_lib`` (plan §1.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import format_param, output_columns, series_id
from finances_core.schemas.ta_lib import (
    MA_TYPE_NAMES,
    TaLibCatalog,
    TaLibCatalogEntry,
    TaLibComputeRequest,
    TaLibOutputInfo,
    TaLibParamInfo,
    TaLibPlan,
    TaLibPlanItem,
    TaLibSpec,
)

DS = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}


# --- TaLibSpec ---------------------------------------------------------------------------


def test_spec_normalizes_function_name_to_upper() -> None:
    spec = TaLibSpec.model_validate({"function": "  rsi "})
    assert spec.function == "RSI"
    assert spec.params == {}
    assert spec.inputs == {}


@pytest.mark.parametrize("name", ["", "rsi-14", "1rsi", "r s i"])
def test_spec_rejects_non_identifier_function(name: str) -> None:
    with pytest.raises(ValidationError):
        TaLibSpec.model_validate({"function": name})


@pytest.mark.parametrize("column", ["open", "high", "low", "close", "adj_close", "volume"])
def test_spec_inputs_accept_allowed_columns(column: str) -> None:
    spec = TaLibSpec.model_validate({"function": "RSI", "inputs": {"price": column}})
    assert spec.inputs == {"price": column}


@pytest.mark.parametrize("column", ["vwap", "Close", "quote_volume", "trades_count", ""])
def test_spec_inputs_reject_other_columns(column: str) -> None:
    with pytest.raises(ValidationError):
        TaLibSpec.model_validate({"function": "RSI", "inputs": {"price": column}})


def test_spec_params_keep_their_json_type() -> None:
    spec = TaLibSpec.model_validate(
        {"function": "bbands", "params": {"timeperiod": 20, "nbdevup": 2.5, "matype": "ema"}}
    )
    assert spec.params == {"timeperiod": 20, "nbdevup": 2.5, "matype": "ema"}
    assert type(spec.params["timeperiod"]) is int
    assert type(spec.params["nbdevup"]) is float


@pytest.mark.parametrize("value", [True, None, [1], {"a": 1}])
def test_spec_params_reject_booleans_and_non_scalars(value: object) -> None:
    with pytest.raises(ValidationError):
        TaLibSpec.model_validate({"function": "rsi", "params": {"timeperiod": value}})


# --- TaLibComputeRequest -----------------------------------------------------------------


def test_request_requires_functions_or_groups() -> None:
    with pytest.raises(ValidationError, match="functions"):
        TaLibComputeRequest.model_validate({"datasets": [DS]})
    with pytest.raises(ValidationError, match="functions"):
        TaLibComputeRequest.model_validate({"datasets": [DS], "functions": [], "groups": []})


def test_request_with_only_groups_is_valid() -> None:
    req = TaLibComputeRequest.model_validate({"datasets": [DS], "groups": ["Pattern Recognition"]})
    assert req.functions == []
    assert req.groups == ["Pattern Recognition"]


def test_request_dataset_limits() -> None:
    with pytest.raises(ValidationError):
        TaLibComputeRequest.model_validate({"datasets": [], "functions": [{"function": "rsi"}]})
    too_many = [{**DS, "symbol": f"S{i}"} for i in range(101)]
    with pytest.raises(ValidationError):
        TaLibComputeRequest.model_validate(
            {"datasets": too_many, "functions": [{"function": "rsi"}]}
        )


#: Ejemplos JSON del plan/spec (RF-5: ``-f rsi:timeperiod=14,price=adj_close -f bbands:...``).
PLAN_REQUEST_EXAMPLE = {
    "datasets": [
        DS,
        {"exchange": "yfinance", "market_type": "equity", "symbol": "AAPL", "timeframe": "1d"},
    ],
    "functions": [
        {"function": "rsi", "params": {"timeperiod": 14}, "inputs": {"price": "adj_close"}},
        {"function": "bbands", "params": {"timeperiod": 20, "matype": "ema"}},
        {"function": "KAMA"},
        {"function": "ht_trendline"},
        {"function": "CDLENGULFING"},
    ],
    "groups": ["Pattern Recognition"],
}


def test_plan_request_example_validates_and_round_trips() -> None:
    req = TaLibComputeRequest.model_validate(PLAN_REQUEST_EXAMPLE)
    assert [f.function for f in req.functions] == [
        "RSI",
        "BBANDS",
        "KAMA",
        "HT_TRENDLINE",
        "CDLENGULFING",
    ]
    again = TaLibComputeRequest.model_validate_json(req.model_dump_json())
    assert again == req


def test_plan_and_catalog_examples_validate() -> None:
    rsi = TaLibSpec.model_validate({"function": "RSI", "params": {"timeperiod": 14}})
    plan = TaLibPlan.model_validate(
        {
            "task_id": "0192f0c4-7d2e-7a3b-8c4d-5e6f7a8b9c0d",
            "created_at_ms": 1_704_067_200_000,
            "items": [
                {
                    "dataset": DS,
                    "series_id": "talib_rsi_14",
                    "spec": rsi.model_dump(),
                    "columns": {"talib_rsi_14": "Float32"},
                    "lookback": 14,
                }
            ],
        }
    )
    assert isinstance(plan.items[0], TaLibPlanItem)
    assert plan.items[0].dataset == DatasetIdentifier.model_validate(DS)

    entry = TaLibCatalogEntry(
        function="BBANDS",
        group="Overlap Studies",
        display_name="Bollinger Bands",
        flags=["Output scale same as input"],
        inputs={"price": ["close"]},
        params=[
            TaLibParamInfo(name="timeperiod", type="int", default=20),
            TaLibParamInfo(name="nbdevup", type="float", default=2.0),
            TaLibParamInfo(name="matype", type="matype", default="sma"),
        ],
        outputs=[
            TaLibOutputInfo(name=n, kind="real", dtype="Float32")
            for n in ("upperband", "middleband", "lowerband")
        ],
        default_lookback=19,
        stability_rule="first_non_null",
        supported=True,
    )
    mavp = entry.model_copy(
        update={"function": "MAVP", "supported": False, "unsupported_reason": "unavailable_input"}
    )
    catalog = TaLibCatalog(
        talib_version="0.8.2",
        ta_lib_c_version="0.8.1",
        groups={"Overlap Studies": ["BBANDS", "MAVP"]},
        functions=[entry, mavp],
    )
    assert TaLibCatalog.model_validate_json(catalog.model_dump_json()) == catalog


def test_output_info_rejects_float64() -> None:
    with pytest.raises(ValidationError):
        TaLibOutputInfo.model_validate({"name": "real", "kind": "real", "dtype": "Float64"})
    with pytest.raises(ValidationError):
        TaLibOutputInfo.model_validate({"name": "real", "kind": "bool", "dtype": "Int16"})


def test_catalog_entry_rejects_unknown_unsupported_reason() -> None:
    with pytest.raises(ValidationError):
        TaLibCatalogEntry.model_validate(
            {
                "function": "X",
                "group": "G",
                "display_name": "X",
                "flags": [],
                "inputs": {},
                "params": [],
                "outputs": [],
                "default_lookback": 0,
                "stability_rule": "first_non_null",
                "supported": False,
                "unsupported_reason": "too_slow",
            }
        )


# --- nomenclatura RF-9 con el núcleo compartido -----------------------------------------


@pytest.mark.parametrize(
    ("name", "params", "overrides", "outputs", "expected"),
    [
        ("RSI", [14], [], [], ["talib_rsi_14"]),
        ("RSI", [14], ["price", "adj_close"], [], ["talib_rsi_14_price_adj_close"]),
        (
            "BBANDS",
            [5, 2.0, 2.0, "sma"],
            [],
            ["upperband"],
            ["talib_bbands_5_2_2_sma_upperband"],
        ),
        (
            "MACD",
            [12, 26, 9],
            [],
            ["macd", "macdsignal"],
            ["talib_macd_12_26_9_macd", "talib_macd_12_26_9_macdsignal"],
        ),
        ("STOCH", [5, 3, "sma", 3, "sma"], [], ["slowk"], ["talib_stoch_5_3_sma_3_sma_slowk"]),
        ("CDLENGULFING", [], [], [], ["talib_cdlengulfing"]),
        ("CDLMORNINGSTAR", [0.3], [], [], ["talib_cdlmorningstar_0p3"]),
        ("MINMAXINDEX", [30], [], ["maxidx"], ["talib_minmaxindex_30_maxidx"]),
    ],
)
def test_spec_naming_examples(
    name: str,
    params: list[int | float | str],
    overrides: list[str],
    outputs: list[str],
    expected: list[str],
) -> None:
    sid = series_id("talib_", name, params, overrides)
    assert output_columns(sid, outputs) == expected


def test_ma_type_names_are_identifiers_in_talib_order() -> None:
    assert MA_TYPE_NAMES[:9] == (
        "sma",
        "ema",
        "wma",
        "dema",
        "tema",
        "trima",
        "kama",
        "mama",
        "t3",
    )
    assert len(set(MA_TYPE_NAMES)) == len(MA_TYPE_NAMES)
    assert all(format_param(n) == n for n in MA_TYPE_NAMES)
