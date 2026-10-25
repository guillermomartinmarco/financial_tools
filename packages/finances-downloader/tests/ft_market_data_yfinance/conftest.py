"""Fixtures de ``ft_market_data_yfinance``: Yahoo simulado vía un ``Ticker`` falso.

Ningún test toca la red: :class:`FakeYahoo` imita ``yfinance.Ticker`` (``history`` con
``auto_adjust=False``/``actions=True`` e índice tz-aware en la zona de la bolsa, más
``history_metadata``) y lanza las mismas excepciones que yfinance 1.7 con
``hide_exceptions = False``. Se inyecta a través de ``adapter.default_fetcher``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import structlog
from finances_core.config import FinancesSettings
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

from finances_downloader.ft_market_data_yfinance import adapter
from finances_downloader.ft_market_data_yfinance.adapter import FetchFn

NO_DATA_REASON = "No data found, symbol may be delisted"

#: Zona horaria de la bolsa e ``instrumentType`` medidos con yfinance 1.7.0.
INSTRUMENTS: dict[str, tuple[str, str]] = {
    "AAPL": ("America/New_York", "EQUITY"),
    "SPY": ("America/New_York", "ETF"),
    "^GSPC": ("America/New_York", "INDEX"),
    "GC=F": ("America/New_York", "FUTURE"),
    "EURUSD=X": ("Europe/London", "CURRENCY"),
    "BTC-USD": ("UTC", "CRYPTOCURRENCY"),
    "ETH-USD": ("UTC", "CRYPTOCURRENCY"),
    "GGAL.BA": ("America/Argentina/Buenos_Aires", "EQUITY"),
}

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def task_id_from(output: str) -> str:
    """Extrae el primer UUID impreso por la CLI (tabla de resultado)."""
    match = _UUID_RE.search(output)
    assert match is not None, output
    return match.group(0)


def ms(year: int, month: int, day: int = 1, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def yahoo_frame(
    index: pd.DatetimeIndex,
    *,
    price: float = 100.0,
    dividends: dict[int, float] | None = None,
    splits: dict[int, float] | None = None,
    capital_gains: dict[int, float] | None = None,
) -> pd.DataFrame:
    """DataFrame con las columnas de ``Ticker.history(auto_adjust=False, actions=True)``.

    ``dividends``/``splits``/``capital_gains`` mapean posición de fila -> valor.
    ``capital_gains`` presente añade la columna ``Capital Gains`` (ETFs).
    """
    n = len(index)
    base = [price + i for i in range(n)]
    data: dict[str, list[float]] = {
        "Open": base,
        "High": [p + 2 for p in base],
        "Low": [p - 2 for p in base],
        "Close": [p + 1 for p in base],
        "Adj Close": [p + 0.5 for p in base],
        "Volume": [1_000_000.0 + i for i in range(n)],
        "Dividends": [0.0] * n,
        "Stock Splits": [0.0] * n,
    }
    for pos, value in (dividends or {}).items():
        data["Dividends"][pos] = value
    for pos, value in (splits or {}).items():
        data["Stock Splits"][pos] = value
    if capital_gains is not None:
        data["Capital Gains"] = [0.0] * n
        for pos, value in capital_gains.items():
            data["Capital Gains"][pos] = value
    return pd.DataFrame(data, index=index)


def business_days(symbol: str, start: str, end: str) -> pd.DatetimeIndex:
    """Velas diarias (lunes-viernes) a medianoche local de la bolsa del símbolo."""
    tz = INSTRUMENTS[symbol][0]
    return pd.bdate_range(start, end, tz=tz, name="Date")


def all_days(symbol: str, start: str, end: str) -> pd.DatetimeIndex:
    tz = INSTRUMENTS[symbol][0]
    return pd.date_range(start, end, freq="D", tz=tz, name="Date")


def intraday(symbol: str, start: str, end: str, freq: str) -> pd.DatetimeIndex:
    tz = INSTRUMENTS[symbol][0]
    return pd.date_range(start, end, freq=freq, tz=tz, inclusive="left", name="Datetime")


class FakeTicker:
    """Imitación mínima de ``yfinance.Ticker`` (``history`` + ``history_metadata``)."""

    def __init__(self, yahoo: FakeYahoo, symbol: str) -> None:
        self.yahoo = yahoo
        self.ticker = symbol
        self.history_metadata: dict[str, Any] = {}

    def history(self, **kwargs: Any) -> pd.DataFrame:
        yahoo = self.yahoo
        yahoo.calls.append({"symbol": self.ticker, **kwargs})
        pending = yahoo.rate_limited.get(self.ticker, 0)
        if pending != 0:
            yahoo.rate_limited[self.ticker] = pending - 1
            raise YFRateLimitError
        if self.ticker in yahoo.network_errors and yahoo.network_errors[self.ticker]:
            raise yahoo.network_errors[self.ticker].pop(0)
        if self.ticker not in INSTRUMENTS or self.ticker in yahoo.delisted:
            raise YFPricesMissingError(self.ticker, "", yahoo_reason=NO_DATA_REASON)

        instrument = yahoo.instrument_override.get(self.ticker, INSTRUMENTS[self.ticker][1])
        self.history_metadata = {"instrumentType": instrument}
        frame = yahoo.frames.get((self.ticker, kwargs["interval"]))
        start = pd.Timestamp(kwargs["start"], unit="s", tz="UTC")
        end = pd.Timestamp(kwargs["end"], unit="s", tz="UTC")
        if frame is None:
            sliced = frame
        else:
            utc_index = pd.DatetimeIndex(frame.index).tz_convert("UTC")
            mask = (utc_index >= start) & (utc_index < end)
            sliced = frame.loc[mask]
        if sliced is None or sliced.empty:
            # yfinance 1.7 sin datos en el rango: "no price data found" (sin "No data found").
            raise YFPricesMissingError(self.ticker, f" ({kwargs['interval']} {start} -> {end})")
        return sliced.copy()


@dataclass
class FakeYahoo:
    """Yahoo Finance en memoria: series por ``(symbol, yahoo_interval)``."""

    frames: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    #: Símbolo -> número de HTTP 429 a lanzar antes de responder (-1 = siempre).
    rate_limited: dict[str, int] = field(default_factory=dict)
    #: Símbolo -> errores de red a lanzar en orden.
    network_errors: dict[str, list[Exception]] = field(default_factory=dict)
    #: Símbolos válidos que Yahoo trata como deslistados ("No data found").
    delisted: set[str] = field(default_factory=set)
    instrument_override: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, symbol: str, interval: str, frame: pd.DataFrame) -> None:
        self.frames[(symbol, interval)] = frame

    def ticker(self, symbol: str) -> FakeTicker:
        return FakeTicker(self, symbol)

    def calls_for(self, symbol: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["symbol"] == symbol]

    @property
    def fetcher(self) -> FetchFn:
        return partial(adapter.fetch_history, ticker_factory=self.ticker)


@pytest.fixture
def fake_yahoo() -> FakeYahoo:
    return FakeYahoo()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FinancesSettings:
    """Settings aislados en ``tmp_path``, sin esperas de backoff ni de rate-limit."""
    monkeypatch.setenv("FT_YFINANCE_BASE_BACKOFF_MS", "0")
    monkeypatch.setenv("FT_YFINANCE_RATE_CAPACITY", "1000")
    monkeypatch.setenv("FT_YFINANCE_RATE_REFILL_RATE", "1000")
    return FinancesSettings()


@pytest.fixture
def use_fake_yahoo(
    monkeypatch: pytest.MonkeyPatch, fake_yahoo: FakeYahoo, settings: FinancesSettings
) -> FakeYahoo:
    """Inyecta :class:`FakeYahoo` como *fetcher* por defecto del adaptador."""

    def factory() -> FetchFn:
        return fake_yahoo.fetcher

    monkeypatch.setattr(adapter, "default_fetcher", factory)
    return fake_yahoo


@pytest.fixture(autouse=True)
def _forbid_real_yahoo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cualquier ``yfinance.Ticker`` real en tests es un error (sin red)."""

    def boom(symbol: str) -> Any:
        msg = f"real yfinance.Ticker({symbol!r}) used in tests"
        raise AssertionError(msg)

    monkeypatch.setattr(adapter, "_real_ticker", boom)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """La CLI reconfigura logging sobre streams de ``CliRunner``: se restaura tras cada test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)
