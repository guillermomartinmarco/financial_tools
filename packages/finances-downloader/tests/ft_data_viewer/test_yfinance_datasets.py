"""yfinance T5.1: el viewer lista y resume datasets ``yfinance`` e ignora ``yfinance_events/``.

Los Parquet se construyen con Polars en el layout que escribe ``ft_market_data_yfinance``
(sin importarla, Art. 4): columnas físicas ``open_time_ms``, OHLC, ``adj_close`` y ``volume``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
import structlog
from finances_core.schemas.data_viewer import DatasetIdentifier
from structlog.testing import capture_logs
from typer.testing import CliRunner

from finances_downloader.ft_data_viewer import service
from finances_downloader.ft_data_viewer.cli import app
from finances_downloader.ft_data_viewer.scanner import MARKET_TYPES

from .conftest import HOUR_MS, write_dataset

DAY_MS = 24 * HOUR_MS
#: 2024-01-02T05:00:00Z = medianoche de Nueva York (EST), vela diaria de AAPL.
AAPL_FIRST_MS = 1_704_171_600_000

YF_FILE_SCHEMA: dict[str, type[pl.DataType]] = {
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "adj_close": pl.Float32,
    "volume": pl.Float32,
}

AAPL = DatasetIdentifier(exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """El CLI reconfigura logging hacia streams de CliRunner que luego se cierran."""
    yield
    logging.basicConfig(force=True, handlers=[logging.NullHandler()])
    structlog.reset_defaults()


def _yf_daily(n: int) -> pl.DataFrame:
    close = [180.0 + i for i in range(n)]
    return pl.DataFrame(
        {
            "open_time_ms": [AAPL_FIRST_MS + i * DAY_MS for i in range(n)],
            "open": [c - 1 for c in close],
            "high": [c + 2 for c in close],
            "low": [c - 2 for c in close],
            "close": close,
            "adj_close": [c * 0.99 for c in close],
            "volume": [5.0e7 for _ in close],
        },
        schema=YF_FILE_SCHEMA,
    )


@pytest.fixture
def yf_tree(parquet_dir: Path) -> Path:
    """``yfinance/market_type=equity/symbol=AAPL/timeframe=1d`` (40 velas, ene→feb 2024)
    más la tabla de eventos ``yfinance_events/symbol=AAPL/events.parquet``."""
    write_dataset(
        parquet_dir,
        _yf_daily(40),
        exchange="yfinance",
        market_type="equity",
        symbol="AAPL",
        timeframe="1d",
    )
    events_dir = parquet_dir / "yfinance_events" / "symbol=AAPL"
    events_dir.mkdir(parents=True)
    pl.DataFrame(
        {
            "event_time_ms": [AAPL_FIRST_MS + 10 * DAY_MS],
            "event_type": ["dividend"],
            "value": [0.24],
        },
        schema={"event_time_ms": pl.Int64, "event_type": pl.Utf8, "value": pl.Float64},
    ).write_parquet(events_dir / "events.parquet")
    return parquet_dir


def test_market_types_include_yahoo_values() -> None:
    assert set(MARKET_TYPES) == {
        "spot",
        "futures",
        "equity",
        "etf",
        "index",
        "fx",
        "crypto",
        "future",
        "fund",
        "other",
    }


def test_list_shows_yfinance_dataset_and_ignores_events_tree(yf_tree: Path) -> None:
    with capture_logs() as logs:
        res = service.list_datasets()
    assert [d.id for d in res.datasets] == [AAPL]
    (info,) = res.datasets
    assert info.partition_path == "yfinance/market_type=equity/symbol=AAPL/timeframe=1d"
    assert info.estimated_rows == 40
    assert info.month_count == 2
    # El árbol de eventos no tiene layout de velas: se ignora sin warnings ruidosos.
    assert not [e for e in logs if e["log_level"] in ("warning", "error")]


def test_list_filters_by_yahoo_market_type(yf_tree: Path) -> None:
    assert service.list_datasets(market_type="equity").total_count == 1
    assert service.list_datasets(exchange="yfinance", symbol="aapl").total_count == 1
    assert service.list_datasets(market_type="spot").total_count == 0


def test_summary_works_on_yfinance_dataset(yf_tree: Path) -> None:
    summary = service.get_summary(service.make_identifier("yfinance", "equity", "AAPL", "1d"))
    assert summary.row_count == 40
    assert summary.date_range == (AAPL_FIRST_MS, AAPL_FIRST_MS + 39 * DAY_MS)
    names = {c.name for c in summary.columns}
    assert {"open", "close", "adj_close", "volume"} <= names


def test_symbols_with_special_characters_resolve(parquet_dir: Path) -> None:
    for market_type, symbol in (("index", "^GSPC"), ("fx", "EURUSD=X"), ("crypto", "BTC-USD")):
        write_dataset(
            parquet_dir,
            _yf_daily(3),
            exchange="yfinance",
            market_type=market_type,
            symbol=symbol,
            timeframe="1d",
        )
        ident = service.make_identifier("yfinance", market_type, symbol, "1d")
        assert service.get_summary(ident).row_count == 3
    assert service.list_datasets(exchange="yfinance").total_count == 3


def test_cli_list_and_summary_on_yfinance(yf_tree: Path) -> None:
    res = runner.invoke(app, ["list", "-f", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert [d["id"] for d in data["datasets"]] == [AAPL.model_dump()]
    summary = runner.invoke(
        app, ["summary", "-e", "yfinance", "-m", "equity", "-s", "AAPL", "-t", "1d", "-f", "json"]
    )
    assert summary.exit_code == 0, summary.output
    assert json.loads(summary.stdout)["row_count"] == 40


def test_cli_help_lists_widened_market_types() -> None:
    res = runner.invoke(app, ["list", "--help"])
    assert res.exit_code == 0
    assert "equity" in res.stdout
