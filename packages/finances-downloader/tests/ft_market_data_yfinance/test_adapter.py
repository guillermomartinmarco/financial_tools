"""T2.2 — Adaptador Yahoo: parámetros de ``history``, pandas→Polars y clasificación de errores."""

from __future__ import annotations

import polars as pl
import pytest
import yfinance as yf
from yfinance.exceptions import YFTzMissingError

from finances_downloader.ft_market_data_yfinance import adapter
from finances_downloader.ft_market_data_yfinance.adapter import (
    SymbolNotFoundError,
    YahooBatch,
    YahooTransientError,
    fetch_history,
    is_retryable,
)

from .conftest import FakeYahoo, business_days, intraday, ms, yahoo_frame


def _aapl(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.add(
        "AAPL",
        "1d",
        yahoo_frame(
            business_days("AAPL", "2020-08-03", "2020-09-04"),
            dividends={4: 0.205},  # 2020-08-07
            splits={20: 4.0},  # 2020-08-31
        ),
    )


def test_history_called_unadjusted_with_actions_and_timeout(fake_yahoo: FakeYahoo) -> None:
    _aapl(fake_yahoo)
    fetch_history(
        "AAPL", "1d", ms(2020, 8, 3), ms(2020, 9, 1), 17, ticker_factory=fake_yahoo.ticker
    )
    (call,) = fake_yahoo.calls
    assert call["auto_adjust"] is False
    assert call["actions"] is True
    assert call["interval"] == "1d"
    assert call["timeout"] == 17
    assert call["start"] == ms(2020, 8, 3) // 1000
    assert call["end"] == ms(2020, 9, 1) // 1000
    assert call.get("repair", False) is False
    assert call.get("prepost", False) is False


def test_exceptions_are_made_visible() -> None:
    adapter.configure_yfinance()
    assert yf.config.debug.hide_exceptions is False


def test_pandas_frame_is_converted_to_polars_with_utc_ms(fake_yahoo: FakeYahoo) -> None:
    _aapl(fake_yahoo)
    batch = fetch_history(
        "AAPL", "1d", ms(2020, 8, 3), ms(2020, 8, 8), 30, ticker_factory=fake_yahoo.ticker
    )
    assert isinstance(batch, YahooBatch)
    assert isinstance(batch.candles, pl.DataFrame)
    assert batch.candles.columns == [
        "open_time_ms",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    ]
    assert batch.candles.schema["open_time_ms"] == pl.Int64
    # 2020-08-03 00:00 America/New_York (EDT, UTC-4) = 04:00 UTC.
    assert batch.candles["open_time_ms"][0] == ms(2020, 8, 3, 4)
    assert batch.candles.height == 5
    assert batch.candles["adj_close"][0] == pytest.approx(100.5)


def test_instrument_type_comes_from_history_metadata(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.add(
        "EURUSD=X", "1h", yahoo_frame(intraday("EURUSD=X", "2026-10-20", "2026-10-21", "1h"))
    )
    batch = fetch_history(
        "EURUSD=X", "1h", ms(2026, 10, 19), ms(2026, 10, 22), 30, ticker_factory=fake_yahoo.ticker
    )
    assert batch.instrument_type == "CURRENCY"


def test_events_are_extracted_only_when_non_zero(fake_yahoo: FakeYahoo) -> None:
    _aapl(fake_yahoo)
    batch = fetch_history(
        "AAPL", "1d", ms(2020, 8, 1), ms(2020, 9, 5), 30, ticker_factory=fake_yahoo.ticker
    )
    rows = batch.events.sort("event_time_ms").to_dicts()
    assert rows == [
        {"event_time_ms": ms(2020, 8, 7, 4), "event_type": "dividend", "value": 0.205},
        {"event_time_ms": ms(2020, 8, 31, 4), "event_type": "split", "value": 4.0},
    ]


def test_intraday_events_are_dated_at_exchange_local_midnight(fake_yahoo: FakeYahoo) -> None:
    index = intraday("AAPL", "2020-08-07 09:30", "2020-08-07 16:00", "1h")
    fake_yahoo.add("AAPL", "1h", yahoo_frame(index, dividends={0: 0.205}))
    batch = fetch_history(
        "AAPL", "1h", ms(2020, 8, 7), ms(2020, 8, 8), 30, ticker_factory=fake_yahoo.ticker
    )
    assert batch.events.to_dicts() == [
        {"event_time_ms": ms(2020, 8, 7, 4), "event_type": "dividend", "value": 0.205}
    ]


def test_capital_gains_column_for_etfs(fake_yahoo: FakeYahoo) -> None:
    fake_yahoo.add(
        "SPY",
        "1d",
        yahoo_frame(business_days("SPY", "2024-12-16", "2024-12-20"), capital_gains={4: 0.12}),
    )
    batch = fetch_history(
        "SPY", "1d", ms(2024, 12, 16), ms(2024, 12, 21), 30, ticker_factory=fake_yahoo.ticker
    )
    assert batch.events["event_type"].to_list() == ["capital_gain"]
    assert batch.instrument_type == "ETF"


def test_nan_prices_become_nulls(fake_yahoo: FakeYahoo) -> None:
    frame = yahoo_frame(business_days("AAPL", "2024-01-02", "2024-01-04"))
    frame.iloc[1, frame.columns.get_loc("Open")] = float("nan")
    fake_yahoo.add("AAPL", "1d", frame)
    batch = fetch_history(
        "AAPL", "1d", ms(2024, 1, 1), ms(2024, 1, 5), 30, ticker_factory=fake_yahoo.ticker
    )
    assert batch.candles["open"].null_count() == 1


def test_rate_limit_429_is_retryable(fake_yahoo: FakeYahoo) -> None:
    _aapl(fake_yahoo)
    fake_yahoo.rate_limited["AAPL"] = 1
    with pytest.raises(YahooTransientError) as info:
        fetch_history(
            "AAPL", "1d", ms(2020, 8, 3), ms(2020, 9, 1), 30, ticker_factory=fake_yahoo.ticker
        )
    assert is_retryable(info.value)
    assert "429" in str(info.value)


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionError("reset")])
def test_network_errors_are_retryable(fake_yahoo: FakeYahoo, error: Exception) -> None:
    _aapl(fake_yahoo)
    fake_yahoo.network_errors["AAPL"] = [error]
    with pytest.raises(YahooTransientError) as info:
        fetch_history(
            "AAPL", "1d", ms(2020, 8, 3), ms(2020, 9, 1), 30, ticker_factory=fake_yahoo.ticker
        )
    assert is_retryable(info.value)


def test_no_data_found_is_symbol_not_found_and_not_retryable(fake_yahoo: FakeYahoo) -> None:
    with pytest.raises(SymbolNotFoundError) as info:
        fetch_history(
            "NOPE123", "1d", ms(2024, 1, 1), ms(2024, 2, 1), 30, ticker_factory=fake_yahoo.ticker
        )
    assert not is_retryable(info.value)
    assert "NOPE123" in str(info.value)


def test_missing_timezone_is_symbol_not_found(fake_yahoo: FakeYahoo) -> None:
    class TzlessTicker:
        history_metadata: dict[str, str] = {}

        def history(self, **_: object) -> None:
            raise YFTzMissingError("ZZZZ")

    with pytest.raises(SymbolNotFoundError):
        fetch_history(
            "ZZZZ",
            "1d",
            ms(2024, 1, 1),
            ms(2024, 2, 1),
            30,
            ticker_factory=lambda _: TzlessTicker(),
        )


def test_empty_range_is_an_empty_batch_not_an_error(fake_yahoo: FakeYahoo) -> None:
    """RF-6: sin velas (fin de semana) -> ventana procesada sin operaciones."""
    _aapl(fake_yahoo)
    batch = fetch_history(
        "AAPL", "1d", ms(2020, 8, 8, 6), ms(2020, 8, 10), 30, ticker_factory=fake_yahoo.ticker
    )
    assert batch.candles.height == 0
    assert batch.events.height == 0
    assert batch.candles.columns[0] == "open_time_ms"


def test_other_errors_propagate(fake_yahoo: FakeYahoo) -> None:
    class Broken:
        history_metadata: dict[str, str] = {}

        def history(self, **_: object) -> None:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        fetch_history("AAPL", "1d", 0, 1, 30, ticker_factory=lambda _: Broken())
    assert not is_retryable(RuntimeError("bug"))


def test_default_fetcher_is_fetch_history() -> None:
    assert adapter.default_fetcher() is fetch_history
