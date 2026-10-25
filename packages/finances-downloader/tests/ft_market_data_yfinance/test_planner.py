"""T2.3 — Plan puro (sin red), incremental, con límites de Yahoo y persistido."""

from __future__ import annotations

import ast
from pathlib import Path

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.market_data_yfinance import YF_OHLCV_SCHEMA, YFDownloadRequest

from finances_downloader.ft_market_data_yfinance import planner
from finances_downloader.ft_market_data_yfinance.planner import (
    build_download_plan,
    load_plan,
    validate_request,
)
from finances_downloader.ft_market_data_yfinance.timeframes import DAY_MS, LOOKBACK_MARGIN_MS

from .conftest import ms

NOW = ms(2026, 10, 25, 12)


def _request(**overrides: object) -> YFDownloadRequest:
    data: dict[str, object] = {
        "symbols": ["AAPL"],
        "timeframes": ["1d"],
        "start_ms": ms(2024, 1, 1),
        "end_ms": ms(2024, 3, 1),
    }
    data.update(overrides)
    return YFDownloadRequest.model_validate(data)


def _write_local(parquet_dir: Path, symbol: str, tf: str, open_times: list[int]) -> None:
    from finances_core.marketdata.months import ms_to_year_month  # noqa: PLC0415

    rows = [
        {
            "symbol": symbol,
            "timeframe": tf,
            "market_type": "equity",
            "open_time_ms": t,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "adj_close": 1.0,
            "volume": 1.0,
            "year": ms_to_year_month(t)[0],
            "month": ms_to_year_month(t)[1],
        }
        for t in open_times
    ]
    write_parquet_partitioned(
        pl.DataFrame(rows), parquet_dir, exchange="yfinance", schema=YF_OHLCV_SCHEMA
    )


def test_fresh_request_plans_one_item_with_yahoo_interval(settings: FinancesSettings) -> None:
    outcome = build_download_plan(
        _request(timeframes=["1w"]), task_id="t-1", settings=settings, now_ms=NOW
    )
    (item,) = outcome.plan.items
    assert (item.symbol, item.timeframe, item.yahoo_interval) == ("AAPL", "1w", "1wk")
    assert (item.start_ms, item.end_ms) == (ms(2024, 1, 1), ms(2024, 3, 1))
    assert outcome.plan.gaps_unfillable == []
    assert outcome.gaps_found == 1


def test_symbols_are_kept_verbatim(settings: FinancesSettings) -> None:
    outcome = build_download_plan(
        _request(symbols=["^GSPC", "EURUSD=X", "GC=F"]), settings=settings, now_ms=NOW
    )
    assert [i.symbol for i in outcome.plan.items] == ["EURUSD=X", "GC=F", "^GSPC"]


def test_existing_data_only_plans_after_last_candle(settings: FinancesSettings) -> None:
    # Velas diarias NY (04:00/05:00 UTC) de enero a mediados de febrero, con fines de semana.
    days = [ms(2024, 1, d, 5) for d in (2, 3, 4, 5, 8, 9, 10, 11, 12, 29, 30, 31)]
    days += [ms(2024, 2, d, 5) for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15)]
    _write_local(settings.PARQUET_DIR, "AAPL", "1d", days)
    last = ms(2024, 2, 15, 5)

    outcome = build_download_plan(
        _request(start_ms=ms(2024, 1, 2, 5)), settings=settings, now_ms=NOW
    )

    # Fines de semana y huecos dentro de cada partición están cubiertos (RF-6).
    (item,) = outcome.plan.items
    assert last < item.start_ms <= last + DAY_MS
    assert item.end_ms == ms(2024, 3, 1)


def test_fully_covered_range_plans_nothing(settings: FinancesSettings) -> None:
    _write_local(settings.PARQUET_DIR, "AAPL", "1d", [ms(2024, 1, 2, 5), ms(2024, 1, 31, 5)])
    outcome = build_download_plan(
        _request(start_ms=ms(2024, 1, 2, 5), end_ms=ms(2024, 1, 31, 12)),
        settings=settings,
        now_ms=NOW,
    )
    assert outcome.plan.items == []
    assert outcome.gaps_found == 0


def test_inventory_considers_any_market_type(settings: FinancesSettings) -> None:
    """RF-1 paso 2: el ``market_type`` en disco lo decide Yahoo, el plan mira todos."""
    _write_local(settings.PARQUET_DIR, "AAPL", "1d", [ms(2024, 1, 2, 5), ms(2024, 1, 31, 5)])
    moved = settings.PARQUET_DIR / "yfinance" / "market_type=equity"
    moved.rename(moved.with_name("market_type=other"))
    outcome = build_download_plan(
        _request(start_ms=ms(2024, 1, 2, 5), end_ms=ms(2024, 1, 31, 12)),
        settings=settings,
        now_ms=NOW,
    )
    assert outcome.plan.items == []


def test_sub_candle_gap_between_partitions_is_not_requested(settings: FinancesSettings) -> None:
    """31-ene + 23 h < 1-feb 05:00: el resto de hora no puede contener una vela diaria."""
    _write_local(
        settings.PARQUET_DIR,
        "AAPL",
        "1d",
        [ms(2024, 1, 30, 5), ms(2024, 1, 31, 5), ms(2024, 2, 1, 5)],
    )
    outcome = build_download_plan(
        _request(start_ms=ms(2024, 1, 30, 5), end_ms=ms(2024, 2, 1, 12)),
        settings=settings,
        now_ms=NOW,
    )
    assert outcome.plan.items == []
    # Un gap final igual de corto sí se pide: puede contener una vela ya cerrada.
    tail = build_download_plan(
        _request(start_ms=ms(2024, 1, 30, 5), end_ms=ms(2024, 2, 2, 12)),
        settings=settings,
        now_ms=NOW,
    )
    assert [(i.start_ms, i.end_ms) for i in tail.plan.items] == [
        (ms(2024, 2, 2, 4), ms(2024, 2, 2, 12))
    ]


def test_beyond_lookback_goes_to_gaps_unfillable(settings: FinancesSettings) -> None:
    request = _request(timeframes=["5m"], start_ms=NOW - 90 * DAY_MS, end_ms=NOW)
    outcome = build_download_plan(request, settings=settings, now_ms=NOW)
    available_from = NOW - 60 * DAY_MS + LOOKBACK_MARGIN_MS
    (gap,) = outcome.plan.gaps_unfillable
    assert (gap.symbol, gap.timeframe, gap.reason) == ("AAPL", "5m", "beyond_yahoo_lookback")
    assert (gap.start_ms, gap.end_ms) == (NOW - 90 * DAY_MS, available_from)
    assert [(i.start_ms, i.end_ms) for i in outcome.plan.items] == [(available_from, NOW)]


def test_1m_items_are_chunked(settings: FinancesSettings) -> None:
    request = _request(timeframes=["1m"], start_ms=NOW - 20 * DAY_MS, end_ms=NOW)
    outcome = build_download_plan(request, settings=settings, now_ms=NOW)
    assert len(outcome.plan.items) == 3
    assert all(i.end_ms - i.start_ms <= 8 * DAY_MS for i in outcome.plan.items)


def test_plan_is_saved_and_loadable(settings: FinancesSettings) -> None:
    outcome = build_download_plan(
        _request(symbols=["AAPL", "BTC-USD"], timeframes=["1d", "1h"]),
        task_id="0192f000-0000-7000-8000-000000000001",
        settings=settings,
        now_ms=NOW,
    )
    path = settings.SHARE_DIR / "plans" / "download_plan_0192f000-0000-7000-8000-000000000001.json"
    assert path.is_file()
    loaded = load_plan("0192f000-0000-7000-8000-000000000001", settings)
    assert loaded == outcome.plan
    assert load_plan("0192f000-0000-7000-8000-00000000ffff", settings) is None


def test_planner_does_not_touch_the_network() -> None:
    tree = ast.parse(Path(planner.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(name.startswith(("yfinance", "pandas")) for name in imported)
    assert not any(name.endswith((".adapter", ".service")) for name in imported)


def test_end_in_future_is_clamped_to_now() -> None:
    assert validate_request(_request(end_ms=NOW + DAY_MS), NOW) == NOW


def test_future_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="Cannot download future data"):
        validate_request(_request(start_ms=NOW + DAY_MS, end_ms=None), NOW)


def test_end_none_means_now() -> None:
    assert validate_request(_request(end_ms=None), NOW) == NOW
