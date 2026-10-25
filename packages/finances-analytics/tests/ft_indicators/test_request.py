"""T2.2: petición — ``--indicator`` vía ``parse_spec_token`` y ``--request-file``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from finances_core.schemas.indicators import IndicatorComputeRequest, IndicatorSpec

from finances_analytics.ft_indicators.errors import IndicatorValidationError
from finances_analytics.ft_indicators.request import (
    build_request,
    dedupe_specs,
    load_request_file,
    spec_from_token,
)


def test_token_produces_same_spec_as_json() -> None:
    from_token = spec_from_token("macd:fast=12,slow=26,signal=9")
    from_json = IndicatorSpec.model_validate_json(
        '{"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}}'
    )
    assert from_token == from_json


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("rsi", IndicatorSpec(name="rsi")),
        ("RSI:period=21", IndicatorSpec(name="rsi", params={"period": 21})),
        ("bb:std=2.5", IndicatorSpec(name="bb", params={"std": 2.5})),
        (
            "sma:period=50,source=ADJ_CLOSE",
            IndicatorSpec(name="sma", params={"period": 50, "source": "adj_close"}),
        ),
    ],
)
def test_tokens(token: str, expected: IndicatorSpec) -> None:
    assert spec_from_token(token) == expected


def test_unknown_indicator_lists_valid_names() -> None:
    with pytest.raises(IndicatorValidationError) as exc:
        spec_from_token("kama:period=10")
    assert "kama" in str(exc.value)
    assert "sma" in str(exc.value) and "vwap" in str(exc.value)


def test_bad_syntax_is_validation_error() -> None:
    with pytest.raises(IndicatorValidationError, match="clave=valor"):
        spec_from_token("rsi:period")


def test_duplicate_specs_are_deduplicated_by_indicator_id() -> None:
    specs = [
        spec_from_token("rsi"),
        spec_from_token("rsi:period=14"),
        spec_from_token("rsi:period=14,source=close"),
        spec_from_token("macd"),
        spec_from_token("rsi:period=21"),
    ]
    unique = dedupe_specs(specs)
    assert [s.name for s in unique] == ["rsi", "macd", "rsi"]
    assert unique[0] == IndicatorSpec(name="rsi", params={"period": 14, "source": "close"})
    assert unique[2].params["period"] == 21


def test_build_request_from_cli_tokens() -> None:
    req = build_request(
        ["binance/spot/BTCUSDT/1h", "yfinance/equity/AAPL/1d"], ["rsi", "macd:fast=5,slow=35"]
    )
    assert [d.path_segment() for d in req.datasets] == [
        "binance/spot/BTCUSDT/1h",
        "yfinance/equity/AAPL/1d",
    ]
    assert req.indicators[1] == IndicatorSpec(name="macd", params={"fast": 5, "slow": 35})


def test_build_request_rejects_bad_dataset() -> None:
    with pytest.raises(IndicatorValidationError, match="dataset"):
        build_request(["binance/spot/BTCUSDT"], ["rsi"])


def test_request_file_is_exactly_the_api_body(tmp_path: Path) -> None:
    body = {
        "datasets": [
            {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}
        ],
        "indicators": [{"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}}],
    }
    path = tmp_path / "req.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    loaded = load_request_file(path)
    assert loaded == IndicatorComputeRequest.model_validate(body)
    assert loaded == build_request(["binance/spot/BTCUSDT/1h"], ["macd:fast=12,slow=26,signal=9"])


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"datasets": [], "indicators": [{"name": "rsi"}]}),
        json.dumps(
            {
                "datasets": [
                    {"exchange": "binance", "market_type": "spot", "symbol": "X", "timeframe": "1h"}
                ],
                "indicators": [{"name": "kama"}],
            }
        ),
    ],
)
def test_invalid_request_file_is_validation_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "req.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(IndicatorValidationError):
        load_request_file(path)


def test_missing_request_file_is_validation_error(tmp_path: Path) -> None:
    with pytest.raises(IndicatorValidationError, match="no existe"):
        load_request_file(tmp_path / "nope.json")
