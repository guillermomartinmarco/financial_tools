"""T3.2 — Velas en Hive ``exchange="yfinance"``; T3.3 — tabla de eventos sin pérdida."""

from __future__ import annotations

from pathlib import Path

import polars as pl
from finances_core.config import FinancesSettings

from finances_downloader.ft_market_data_yfinance.adapter import fetch_history
from finances_downloader.ft_market_data_yfinance.events import events_file, write_events
from finances_downloader.ft_market_data_yfinance.normalizer import normalize_yahoo_batch
from finances_downloader.ft_market_data_yfinance.service import write_candles

from .conftest import FakeYahoo, business_days, ms, yahoo_frame


def _batch_df(fake: FakeYahoo, start: int, end: int) -> pl.DataFrame:
    batch = fetch_history("AAPL", "1d", start, end, 30, ticker_factory=fake.ticker)
    return normalize_yahoo_batch(batch, symbol="AAPL", timeframe="1d", start_ms=start, end_ms=end)


# ------------------------------------------------------------------ T3.2
def test_candles_are_written_in_yfinance_hive_layout(
    fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    fake_yahoo.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-02-29")))
    paths = write_candles(
        _batch_df(fake_yahoo, ms(2024, 1, 1), ms(2024, 3, 1)), settings.PARQUET_DIR
    )
    expected = (
        settings.PARQUET_DIR
        / "yfinance"
        / "market_type=equity"
        / "symbol=AAPL"
        / "timeframe=1d"
        / "year=2024"
        / "month=01"
        / "data.parquet"
    )
    assert str(expected) in paths
    assert len(paths) == 2
    stored = pl.read_parquet(expected)
    assert stored.columns == [
        "open_time_ms",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    ]
    assert stored.height == 22  # días hábiles de enero 2024 (sin feriados en el fake)


def test_partial_rerun_does_not_lose_rows(
    fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    fake_yahoo.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-01-31")))
    write_candles(_batch_df(fake_yahoo, ms(2024, 1, 1), ms(2024, 2, 1)), settings.PARQUET_DIR)
    # Re-ejecución con un rango menor y precios corregidos: no borra el resto del mes.
    fake_yahoo.add(
        "AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-08", "2024-01-12"), price=500.0)
    )
    (path,) = write_candles(
        _batch_df(fake_yahoo, ms(2024, 1, 8), ms(2024, 1, 13)), settings.PARQUET_DIR
    )
    stored = pl.read_parquet(path)
    assert stored.height == 22
    assert stored["open_time_ms"].is_sorted()
    assert stored.filter(pl.col("open_time_ms") == ms(2024, 1, 8, 5))["open"][0] == 500.0
    assert stored.filter(pl.col("open_time_ms") == ms(2024, 1, 2, 5))["open"][0] == 100.0


def test_symbols_with_special_characters_are_written_verbatim(
    fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    for symbol in ("^GSPC", "EURUSD=X", "GC=F"):
        fake_yahoo.add(symbol, "1d", yahoo_frame(business_days(symbol, "2024-01-02", "2024-01-05")))
        batch = fetch_history(
            symbol, "1d", ms(2024, 1, 1), ms(2024, 1, 8), 30, ticker_factory=fake_yahoo.ticker
        )
        df = normalize_yahoo_batch(
            batch, symbol=symbol, timeframe="1d", start_ms=ms(2024, 1, 1), end_ms=ms(2024, 1, 8)
        )
        (path,) = write_candles(df, settings.PARQUET_DIR)
        assert f"symbol={symbol}" in Path(path).parts[-5]


# ------------------------------------------------------------------ T3.3
def _events(rows: list[tuple[int, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"event_time_ms": pl.Int64, "event_type": pl.Utf8, "value": pl.Float64},
        orient="row",
    )


def test_dividend_and_split_written_to_events_table(
    fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> None:
    fake_yahoo.add(
        "AAPL",
        "1d",
        yahoo_frame(
            business_days("AAPL", "2020-08-03", "2020-09-04"),
            dividends={4: 0.205},
            splits={20: 4.0},
        ),
    )
    batch = fetch_history(
        "AAPL", "1d", ms(2020, 8, 1), ms(2020, 9, 5), 30, ticker_factory=fake_yahoo.ticker
    )
    path = write_events(batch.events, settings.PARQUET_DIR, "AAPL")
    expected = settings.PARQUET_DIR / "yfinance_events" / "symbol=AAPL" / "events.parquet"
    assert path == str(expected)
    assert events_file(settings.PARQUET_DIR, "AAPL") == expected
    stored = pl.read_parquet(expected)
    assert stored.schema == {
        "event_time_ms": pl.Int64,
        "event_type": pl.Utf8,
        "value": pl.Float64,
    }
    assert stored.to_dicts() == [
        {"event_time_ms": ms(2020, 8, 7, 4), "event_type": "dividend", "value": 0.205},
        {"event_time_ms": ms(2020, 8, 31, 4), "event_type": "split", "value": 4.0},
    ]


def test_capital_gain_for_etf(fake_yahoo: FakeYahoo, settings: FinancesSettings) -> None:
    fake_yahoo.add(
        "SPY",
        "1d",
        yahoo_frame(
            business_days("SPY", "2024-12-16", "2024-12-20"),
            dividends={4: 1.966},
            capital_gains={4: 0.12},
        ),
    )
    batch = fetch_history(
        "SPY", "1d", ms(2024, 12, 16), ms(2024, 12, 21), 30, ticker_factory=fake_yahoo.ticker
    )
    path = write_events(batch.events, settings.PARQUET_DIR, "SPY")
    assert path is not None
    stored = pl.read_parquet(path)
    assert sorted(stored["event_type"].to_list()) == ["capital_gain", "dividend"]


def test_rerun_does_not_duplicate_and_merges_losslessly(settings: FinancesSettings) -> None:
    first = _events([(ms(2020, 8, 7, 4), "dividend", 0.205), (ms(2020, 8, 31, 4), "split", 4.0)])
    write_events(first, settings.PARQUET_DIR, "AAPL")
    second = _events(
        [
            (ms(2020, 8, 7, 4), "dividend", 0.205),  # repetido
            (ms(2020, 8, 7, 4), "split", 2.0),  # mismo día, otro tipo: se conserva
            (ms(2020, 11, 6, 5), "dividend", 0.205),  # nuevo
        ]
    )
    path = write_events(second, settings.PARQUET_DIR, "AAPL")
    assert path is not None
    stored = pl.read_parquet(path)
    assert stored.height == 4
    assert stored["event_time_ms"].is_sorted()


def test_batch_without_events_does_not_create_file(settings: FinancesSettings) -> None:
    assert write_events(_events([]), settings.PARQUET_DIR, "AAPL") is None
    assert not events_file(settings.PARQUET_DIR, "AAPL").exists()


def test_batch_without_events_does_not_modify_existing_file(settings: FinancesSettings) -> None:
    path = write_events(
        _events([(ms(2020, 8, 7, 4), "dividend", 0.205)]), settings.PARQUET_DIR, "AAPL"
    )
    assert path is not None
    before = Path(path).stat().st_mtime_ns
    assert write_events(_events([]), settings.PARQUET_DIR, "AAPL") is None
    assert Path(path).stat().st_mtime_ns == before
