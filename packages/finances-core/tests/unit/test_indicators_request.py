"""T1.4 (ft_indicators): ``DatasetIdentifier.parse``/``path_segment`` y ``parse_spec_token``."""

from __future__ import annotations

import pytest

from finances_core.indicators_store.request import SpecTokenError, parse_spec_token
from finances_core.schemas.data_viewer import DatasetIdentifier

# --- DatasetIdentifier.parse / path_segment ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "binance/spot/BTCUSDT/1h",
        "binance/futures/ETHUSDT/1m",
        "yfinance/equity/AAPL/1d",
        "yfinance/fx/EURUSD=X/1h",
        "yfinance/index/^GSPC/1d",
    ],
)
def test_parse_path_segment_roundtrip(text: str) -> None:
    ds = DatasetIdentifier.parse(text)
    assert ds.path_segment() == text
    assert DatasetIdentifier.parse(ds.path_segment()) == ds


def test_parse_fields() -> None:
    ds = DatasetIdentifier.parse(" yfinance/equity/AAPL/1d ")
    assert (ds.exchange, ds.market_type, ds.symbol, ds.timeframe) == (
        "yfinance",
        "equity",
        "AAPL",
        "1d",
    )


@pytest.mark.parametrize(
    "text",
    ["", "binance", "binance/spot/BTCUSDT", "binance/spot//1h", "a/b/c/d/e", "/spot/BTC/1h"],
)
def test_parse_rejects_missing_segments(text: str) -> None:
    with pytest.raises(ValueError, match="exchange/market_type/symbol/timeframe"):
        DatasetIdentifier.parse(text)


def test_parse_rejects_invalid_market_type() -> None:
    with pytest.raises(ValueError, match="market_type"):
        DatasetIdentifier.parse("binance/margin/BTCUSDT/1h")


# --- parse_spec_token ----------------------------------------------------------------


def test_bare_name() -> None:
    spec = parse_spec_token("rsi")
    assert spec.name == "rsi"
    assert spec.params == {}


def test_ints_decimals_negatives_identifiers() -> None:
    spec = parse_spec_token("macd:fast=12,slow=26,signal=9")
    assert spec.params == {"fast": 12, "slow": 26, "signal": 9}
    assert all(isinstance(v, int) for v in spec.params.values())

    spec = parse_spec_token("bb:period=20,std=2.5,source=adj_close")
    assert spec.params == {"period": 20, "std": 2.5, "source": "adj_close"}
    assert isinstance(spec.params["std"], float)

    spec = parse_spec_token("x:a=-3,b=-0.5,c=.25,d=1e-3")
    assert spec.params == {"a": -3, "b": -0.5, "c": 0.25, "d": 0.001}


def test_case_kept_only_in_name() -> None:
    spec = parse_spec_token(" BBANDS:TimePeriod=20,MAType=EMA ")
    assert spec.name == "BBANDS"
    assert spec.params == {"timeperiod": 20, "matype": "ema"}


def test_whitespace_around_tokens_is_tolerated() -> None:
    assert parse_spec_token("sma: period = 50 , source = close").params == {
        "period": 50,
        "source": "close",
    }


@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        ("", "nombre"),
        (":period=3", "nombre"),
        ("9rsi", "nombre"),
        ("rsi:", "parámetro"),
        ("rsi:period", "clave=valor"),
        ("rsi:period=", "valor"),
        ("rsi:=14", "clave"),
        ("rsi:period=14,", "parámetro"),
        ("rsi:period=14,period=15", "repetido"),
        ("rsi:period=1.2.3", "valor"),
        ("rsi:source=adj-close", "valor"),
        ("rsi period=14", "nombre"),
    ],
)
def test_invalid_syntax_has_clear_message(token: str, fragment: str) -> None:
    with pytest.raises(SpecTokenError, match=fragment) as exc:
        parse_spec_token(token)
    assert isinstance(exc.value, ValueError)
    assert "name[:k=v,...]" in str(exc.value)
