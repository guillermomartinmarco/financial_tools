"""T2.4: ``--function``/``--group``/``--request-file`` → ``TaLibComputeRequest``; expansión y
deduplicación (RF-5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from finances_core.schemas.ta_lib import TaLibComputeRequest, TaLibSpec

from finances_analytics.ft_ta_lib import catalog, request
from finances_analytics.ft_ta_lib.errors import TaLibValidationError
from finances_analytics.ft_ta_lib.model import ResolvedFunction

DS = "binance/spot/BTCUSDT/1h"
DS_JSON = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}


def test_token_separates_input_keys_from_params() -> None:
    spec = request.spec_from_token("rsi:timeperiod=10,price=adj_close")
    assert spec == TaLibSpec.model_validate(
        {"function": "RSI", "params": {"timeperiod": 10}, "inputs": {"price": "adj_close"}}
    )
    bb = request.spec_from_token("bbands:timeperiod=20,matype=ema")
    assert bb.params == {"timeperiod": 20, "matype": "ema"}
    assert bb.inputs == {}


def test_token_composite_input_key_goes_to_inputs_and_fails_on_resolve() -> None:
    spec = request.spec_from_token("stoch:prices=close")
    assert spec.inputs == {"prices": "close"}
    req = request.build_request([DS], ["stoch:prices=close"], [])
    with pytest.raises(TaLibValidationError, match="compuesta"):
        request.expand(req)


def test_token_with_invalid_input_column_is_validation() -> None:
    with pytest.raises(TaLibValidationError, match="price"):
        request.spec_from_token("rsi:price=vwap")


def test_function_names_are_case_insensitive() -> None:
    a = request.expand(request.build_request([DS], ["RSI", "rsi", "Rsi:timeperiod=14"], []))
    assert [f.series_id for f in a] == ["talib_rsi_14"]


def test_unknown_function_or_group_lists_valid_groups() -> None:
    with pytest.raises(TaLibValidationError, match="Pattern Recognition"):
        request.spec_from_token("nope")
    with pytest.raises(TaLibValidationError, match="Overlap Studies"):
        request.expand(request.build_request([DS], [], ["Nope Studies"]))
    with pytest.raises(TaLibValidationError, match="Momentum Indicators"):
        request.expand(
            request.request_from_payload(
                {"datasets": [DS_JSON], "functions": [{"function": "nope"}]}
            )
        )


def test_syntax_errors_are_validation() -> None:
    with pytest.raises(TaLibValidationError, match="clave=valor"):
        request.spec_from_token("rsi:timeperiod")


def test_pattern_group_expands_to_its_61_supported_functions() -> None:
    fns = request.expand(request.build_request([DS], [], ["Pattern Recognition"]))
    assert len(fns) == 61
    assert {f.name for f in fns} == set(catalog.groups()["Pattern Recognition"])
    assert all(f.series_id.startswith("talib_cdl") for f in fns)


def test_group_names_are_case_insensitive_and_groups_dedupe_with_functions() -> None:
    fns = request.expand(
        request.build_request([DS], ["cdldoji", "CDLDOJI"], ["pattern recognition"])
    )
    assert len(fns) == 61
    assert fns[0].series_id == "talib_cdldoji"


def test_group_expansion_skips_unsupported_functions() -> None:
    fns = request.expand(request.build_request([DS], [], ["Overlap Studies"]))
    names = {f.name for f in fns}
    assert "MAVP" not in names
    assert len(names) == len(catalog.groups()["Overlap Studies"]) - 1


def test_more_than_200_functions_is_validation() -> None:
    groups = list(catalog.groups())
    # ta-lib 0.8.2 / C 0.8.1: 201 funciones, 200 soportadas (todas salvo MAVP).
    everything = request.expand(request.build_request([DS], [], groups))
    assert len(everything) == request.MAX_FUNCTIONS
    with pytest.raises(TaLibValidationError, match="200"):
        request.expand(request.build_request([DS], ["sma:timeperiod=7"], groups))


def test_200_distinct_parametrizations_are_allowed_201_are_not() -> None:
    tokens = [f"sma:timeperiod={p}" for p in range(2, 202)]
    assert len(request.expand(request.build_request([DS], tokens, []))) == 200
    with pytest.raises(TaLibValidationError, match="200"):
        request.expand(request.build_request([DS], [*tokens, "sma:timeperiod=500"], []))


def test_dataset_errors_and_limits_are_validation() -> None:
    with pytest.raises(TaLibValidationError, match="dataset"):
        request.build_request(["binance/spot"], ["rsi"], [])
    with pytest.raises(TaLibValidationError):
        request.build_request([], ["rsi"], [])
    with pytest.raises(TaLibValidationError, match="functions"):
        request.build_request([DS], [], [])


def test_request_file_equals_api_body(tmp_path: Path) -> None:
    body = {
        "datasets": [DS_JSON],
        "functions": [
            {"function": "rsi", "params": {"timeperiod": 10}, "inputs": {"price": "adj_close"}},
            {"function": "bbands", "params": {"timeperiod": 20, "matype": "ema"}},
        ],
        "groups": ["Cycle Indicators"],
    }
    path = tmp_path / "req.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    from_file = request.load_request_file(path)
    assert from_file == TaLibComputeRequest.model_validate(body)
    from_cli = request.build_request(
        [DS],
        ["rsi:timeperiod=10,price=adj_close", "bbands:timeperiod=20,matype=ema"],
        ["Cycle Indicators"],
    )
    assert from_cli == from_file


def test_request_file_errors(tmp_path: Path) -> None:
    with pytest.raises(TaLibValidationError, match="no existe"):
        request.load_request_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TaLibValidationError, match="inválido"):
        request.load_request_file(bad)
    with pytest.raises(TaLibValidationError, match="petición inválida"):
        request.request_from_payload({"datasets": [DS_JSON], "functions": "rsi"})


def test_expand_probes_every_function(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = request.probe

    def spy(fn: ResolvedFunction) -> ResolvedFunction:
        seen.append(fn.series_id)
        return real(fn)

    monkeypatch.setattr(request, "probe", spy)
    request.expand(request.build_request([DS], ["rsi", "macd", "rsi"], []))
    assert seen == ["talib_rsi_14", "talib_macd_12_26_9"]
