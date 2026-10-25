"""T2.1 — Mapa canónico↔Yahoo, límites de historial (RF-3) y ``split_by_limits``."""

from __future__ import annotations

import pytest

from finances_downloader.ft_market_data_yfinance import timeframes
from finances_downloader.ft_market_data_yfinance.timeframes import (
    DAY_MS,
    LOOKBACK_MARGIN_MS,
    YAHOO_INTERVAL,
    split_by_limits,
)

from .conftest import ms

NOW = ms(2026, 10, 25, 12)


def test_canonical_to_yahoo_interval_map() -> None:
    assert YAHOO_INTERVAL == {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1h",
        "1d": "1d",
        "1w": "1wk",
        "1M": "1mo",
    }


def test_limits_table_matches_rf3() -> None:
    assert timeframes.LOOKBACK_DAYS == {"1m": 30, "5m": 60, "15m": 60, "30m": 60, "1h": 730}
    assert timeframes.MAX_WINDOW_DAYS == {"1m": 8, "5m": 60, "15m": 60, "30m": 60, "1h": 730}
    assert LOOKBACK_MARGIN_MS == 3_600_000


def test_1m_of_20_days_is_split_in_windows_of_at_most_8_days() -> None:
    gap = (NOW - 20 * DAY_MS, NOW)
    windows, unfillable = split_by_limits(gap, "1m", NOW)
    assert unfillable == []
    assert len(windows) == 3
    assert all(end - start <= 8 * DAY_MS for start, end in windows)
    # Las ventanas son contiguas y cubren exactamente el gap.
    assert windows[0][0] == gap[0]
    assert windows[-1][1] == gap[1]
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:], strict=False))


def test_5m_from_90_days_ago_is_partly_beyond_lookback() -> None:
    gap = (NOW - 90 * DAY_MS, NOW)
    windows, unfillable = split_by_limits(gap, "5m", NOW)
    available_from = NOW - 60 * DAY_MS + LOOKBACK_MARGIN_MS
    assert unfillable == [(gap[0], available_from)]
    assert windows == [(available_from, NOW)]


def test_gap_entirely_beyond_lookback_produces_no_window() -> None:
    gap = (NOW - 400 * DAY_MS, NOW - 100 * DAY_MS)
    windows, unfillable = split_by_limits(gap, "15m", NOW)
    assert windows == []
    assert unfillable == [gap]


def test_1d_of_30_years_is_a_single_request() -> None:
    gap = (ms(1996, 1, 1), NOW)
    for tf in ("1d", "1w", "1M"):
        windows, unfillable = split_by_limits(gap, tf, NOW)
        assert windows == [gap]
        assert unfillable == []


def test_1h_window_is_capped_at_730_days() -> None:
    gap = (NOW - 700 * DAY_MS, NOW)
    windows, unfillable = split_by_limits(gap, "1h", NOW)
    assert unfillable == []
    assert windows == [gap]


def test_empty_gap_produces_nothing() -> None:
    assert split_by_limits((NOW, NOW), "1m", NOW) == ([], [])


@pytest.mark.parametrize(
    ("tf", "expected"),
    [("1m", 60_000), ("1h", 3_600_000), ("1d", 23 * 3_600_000), ("1M", 28 * DAY_MS)],
)
def test_min_candle_span_tolerates_dst_and_short_months(tf: str, expected: int) -> None:
    """La cobertura usa la duración mínima de vela (DST de 23 h, febrero de 28 días)."""
    assert timeframes.min_candle_ms(tf) == expected


def test_max_candle_span_for_forming_candles() -> None:
    assert timeframes.max_candle_ms("1d") == 25 * 3_600_000
    assert timeframes.max_candle_ms("1M") == 31 * DAY_MS
    assert timeframes.max_candle_ms("5m") == 300_000


def test_events_only_from_timeframes_with_real_event_dates() -> None:
    assert frozenset({"1m", "5m", "15m", "30m", "1h", "1d"}) == timeframes.EVENT_TIMEFRAMES
