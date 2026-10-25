"""T3.1 — Normalización canónica RF-4 (UTC ms, float32, ``adj_close``, ``market_type``)."""

from __future__ import annotations

import pandas as pd
import polars as pl
import pytest
from finances_core.schemas.market_data_yfinance import YF_OHLCV_SCHEMA
from structlog.testing import capture_logs

from finances_downloader.ft_market_data_yfinance.adapter import YahooBatch, fetch_history
from finances_downloader.ft_market_data_yfinance.normalizer import (
    normalize_yahoo_batch,
    resolve_market_type,
)

from .conftest import FakeYahoo, all_days, business_days, intraday, ms, yahoo_frame


def _normalize(
    fake: FakeYahoo, symbol: str, interval: str, tf: str, start: int, end: int, **kw: int
) -> pl.DataFrame:
    batch = fetch_history(symbol, interval, start, end, 30, ticker_factory=fake.ticker)
    return normalize_yahoo_batch(
        batch, symbol=symbol, timeframe=tf, start_ms=start, end_ms=end, **kw
    )


def test_aapl_daily_new_york_midnight_in_utc(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-03-15")))
    df = _normalize(fake_yahoo, "AAPL", "1d", "1d", ms(2024, 1, 1), ms(2024, 4, 1))
    times = df["open_time_ms"].to_list()
    assert times[0] == ms(2024, 1, 2, 5)  # EST (UTC-5)
    assert ms(2024, 3, 15, 4) in times  # EDT (UTC-4) tras el cambio de hora
    assert df["market_type"].unique().to_list() == ["equity"]


def test_eurusd_hourly_london(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.add(
        "EURUSD=X", "1h", yahoo_frame(intraday("EURUSD=X", "2024-07-01", "2024-07-02", "1h"))
    )
    df = _normalize(fake_yahoo, "EURUSD=X", "1h", "1h", ms(2024, 6, 30), ms(2024, 7, 3))
    assert df["open_time_ms"][0] == ms(2024, 6, 30, 23)  # 00:00 BST = 23:00 UTC
    assert df.height == 24
    assert df["market_type"][0] == "fx"
    assert df["symbol"][0] == "EURUSD=X"


def test_btc_usd_daily_utc(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.add("BTC-USD", "1d", yahoo_frame(all_days("BTC-USD", "2024-01-01", "2024-01-07")))
    df = _normalize(fake_yahoo, "BTC-USD", "1d", "1d", ms(2024, 1, 1), ms(2024, 1, 8))
    assert df["open_time_ms"].to_list() == [ms(2024, 1, d) for d in range(1, 8)]
    assert df["market_type"][0] == "crypto"


def test_dtypes_and_columns_follow_schema(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-01-05")))
    df = _normalize(fake_yahoo, "AAPL", "1d", "1d", ms(2024, 1, 1), ms(2024, 1, 8))
    for name, dtype in YF_OHLCV_SCHEMA.items():
        assert df.schema[name] == dtype, name
    assert df.schema["year"] == pl.Int32
    assert df.schema["month"] == pl.Int32
    assert df["adj_close"][0] == pytest.approx(100.5)
    assert df["timeframe"][0] == "1d"
    assert (df["year"][0], df["month"][0]) == (2024, 1)


def test_filters_range_drops_null_ohlc_and_dedupes(fake_yahoo: FakeYahoo) -> None:
    index = business_days("AAPL", "2024-01-02", "2024-01-10")
    frame = yahoo_frame(index)
    frame.iloc[2, frame.columns.get_loc("Close")] = float("nan")
    # Duplicado por open_time: se conserva la última fila.
    dup = frame.iloc[[0]].copy()
    dup["Close"] = 999.0
    fake_yahoo.add("AAPL", "1d", pd.concat([frame, dup]))
    df = _normalize(fake_yahoo, "AAPL", "1d", "1d", ms(2024, 1, 2), ms(2024, 1, 9))
    times = df["open_time_ms"].to_list()
    assert times == sorted(times)
    assert ms(2024, 1, 4, 5) not in times  # OHLC nulo
    assert all(ms(2024, 1, 2) <= t < ms(2024, 1, 9) for t in times)
    assert df.filter(pl.col("open_time_ms") == ms(2024, 1, 2, 5))["close"][0] == 999.0


def test_forming_candle_is_dropped(fake_yahoo: FakeYahoo) -> None:
    """La vela en curso no se persiste: la re-ejecución la pedirá completa."""
    fake_yahoo.add("BTC-USD", "1d", yahoo_frame(all_days("BTC-USD", "2024-01-01", "2024-01-05")))
    now = ms(2024, 1, 5, 12)
    df = _normalize(fake_yahoo, "BTC-USD", "1d", "1d", ms(2024, 1, 1), now, now_ms=now)
    assert df["open_time_ms"].max() == ms(2024, 1, 4)
    df_all = _normalize(fake_yahoo, "BTC-USD", "1d", "1d", ms(2024, 1, 1), now)
    assert df_all["open_time_ms"].max() == ms(2024, 1, 5)


@pytest.mark.parametrize(
    ("instrument", "expected"),
    [
        ("EQUITY", "equity"),
        ("ETF", "etf"),
        ("INDEX", "index"),
        ("CURRENCY", "fx"),
        ("CRYPTOCURRENCY", "crypto"),
        ("FUTURE", "future"),
        ("MUTUALFUND", "fund"),
    ],
)
def test_instrument_type_map(instrument: str, expected: str) -> None:
    assert resolve_market_type(instrument, "X") == expected


def test_unknown_instrument_type_is_other_with_warning(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.instrument_override["AAPL"] = "WARRANT"
    fake_yahoo.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-01-05")))
    with capture_logs() as logs:
        df = _normalize(fake_yahoo, "AAPL", "1d", "1d", ms(2024, 1, 1), ms(2024, 1, 8))
    assert df["market_type"].unique().to_list() == ["other"]
    assert any(e["log_level"] == "warning" and e.get("instrument_type") == "WARRANT" for e in logs)


def test_missing_instrument_type_is_other() -> None:
    assert resolve_market_type(None, "X") == "other"


def test_empty_batch_normalizes_to_empty_frame() -> None:
    batch = YahooBatch.empty()
    df = normalize_yahoo_batch(batch, symbol="AAPL", timeframe="1d", start_ms=0, end_ms=1)
    assert df.height == 0
    assert set(YF_OHLCV_SCHEMA) <= set(df.columns)
