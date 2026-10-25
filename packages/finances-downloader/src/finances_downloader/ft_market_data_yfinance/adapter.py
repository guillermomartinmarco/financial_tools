"""Adaptador Yahoo Finance (plan §3.3, ADR-Y01/Y02).

Único módulo de la herramienta que importa ``yfinance`` y ``pandas``: convierte la
respuesta de ``Ticker.history`` a Polars en el borde y clasifica los errores de
Yahoo en excepciones propias de la herramienta. Es **síncrono**; el servicio lo
ejecuta con ``asyncio.to_thread`` (Art. 3).

Inyección para tests (sin red): :func:`default_fetcher` es el punto de sustitución
(como ``default_transport`` en binance) y :func:`fetch_history` acepta un
``ticker_factory`` que imita ``yfinance.Ticker``.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import polars as pl
import structlog
import yfinance as yf
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError, YFTzMissingError

logger = structlog.get_logger(__name__)

#: Columnas de ``history(auto_adjust=False)`` -> columnas canónicas.
PRICE_COLUMNS: dict[str, str] = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}

#: Columnas de acciones corporativas (``actions=True``) -> ``event_type``.
EVENT_COLUMNS: dict[str, str] = {
    "Dividends": "dividend",
    "Stock Splits": "split",
    "Capital Gains": "capital_gain",  # sólo ETFs / fondos
}

#: Texto de Yahoo para un símbolo inexistente o deslistado (``YFPricesMissingError``).
NO_DATA_MARKER = "no data found"

CANDLE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "open_time_ms": pl.Int64,
    **dict.fromkeys(PRICE_COLUMNS.values(), pl.Float64),
}
EVENT_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "event_time_ms": pl.Int64,
    "event_type": pl.Utf8,
    "value": pl.Float64,
}


class YahooError(Exception):
    """Error de Yahoo Finance clasificado por el adaptador."""


class YahooTransientError(YahooError):
    """Rate-limit (HTTP 429) o error de red/timeout: reintentable (RF-7)."""


class SymbolNotFoundError(YahooError):
    """Símbolo inexistente o deslistado ("No data found"): no reintentable (RF-10)."""

    def __init__(self, symbol: str, detail: str) -> None:
        super().__init__(f"Símbolo sin datos en Yahoo: {symbol} ({detail})")
        self.symbol = symbol


@dataclass(frozen=True)
class YahooBatch:
    """Respuesta de un pedido ya en Polars.

    - ``candles``: ``open_time_ms`` (ms UTC) + ``open/high/low/close/adj_close/volume``.
    - ``events``: ``event_time_ms`` (medianoche local de la bolsa en ms UTC),
      ``event_type`` y ``value``; sólo valores distintos de cero.
    - ``instrument_type``: ``history_metadata["instrumentType"]``.
    """

    candles: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=CANDLE_SCHEMA))
    events: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=EVENT_SCHEMA))
    instrument_type: str | None = None

    @classmethod
    def empty(cls, instrument_type: str | None = None) -> YahooBatch:
        """Ventana sin operaciones (mercado cerrado, RF-6)."""
        return cls(instrument_type=instrument_type)


#: ``(symbol, yahoo_interval, start_ms, end_ms, timeout_s) -> YahooBatch``.
FetchFn = Callable[[str, str, int, int, int], YahooBatch]
TickerFactory = Callable[[str], Any]


def configure_yfinance() -> None:
    """Excepciones visibles (``raise_errors`` está deprecado en yfinance 1.7).

    También silencia las deprecaciones de pandas emitidas *dentro* de yfinance
    (``inplace`` en pandas 3.1): ruido en stderr que no depende de esta herramienta.
    """
    yf.config.debug.hide_exceptions = False
    warnings.filterwarnings("ignore", message=r"The inplace keyword", module=r"yfinance(\.|$)")


def _real_ticker(symbol: str) -> Any:
    return yf.Ticker(symbol)


def is_retryable(exc: BaseException) -> bool:
    """Predicado de reintento RF-7: sólo rate-limit y errores de red/timeout."""
    return isinstance(exc, YahooTransientError)


def _instrument_type(ticker: Any) -> str | None:
    try:
        value = ticker.history_metadata.get("instrumentType")
    except (AttributeError, KeyError, TypeError):
        return None
    return str(value) if value else None


def _utc_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:  # yfinance siempre devuelve tz de la bolsa; por robustez, UTC
        index = index.tz_localize("UTC")
    return index


def _epoch_ms(index: pd.DatetimeIndex) -> pl.Series:
    """Instantes tz-aware -> ms epoch UTC (independiente de la resolución de pandas 3)."""
    naive_utc = index.tz_convert("UTC").tz_localize(None).as_unit("ms")
    return pl.Series("ms", naive_utc.to_numpy()).cast(pl.Int64)


def _float_column(frame: pd.DataFrame, column: str) -> pl.Series:
    if column not in frame.columns:
        return pl.Series(column, [None] * len(frame), dtype=pl.Float64)
    values = frame[column].astype("float64").to_numpy()
    return pl.Series(column, values, dtype=pl.Float64, nan_to_null=True)


def to_polars(frame: pd.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``(candles, events)`` en Polars a partir del DataFrame de ``Ticker.history``."""
    if frame.empty:
        return YahooBatch().candles, YahooBatch().events
    index = _utc_index(frame)
    candles = pl.DataFrame(
        [_epoch_ms(index).alias("open_time_ms")]
        + [_float_column(frame, src).alias(dst) for src, dst in PRICE_COLUMNS.items()]
    )
    # Eventos fechados en la medianoche local de la bolsa: intradía y diario coinciden.
    event_times = _epoch_ms(index.normalize()).alias("event_time_ms")
    parts = [
        pl.DataFrame([event_times, _float_column(frame, src).alias("value")])
        .with_columns(pl.lit(event_type).alias("event_type"))
        .filter(pl.col("value").is_not_null() & (pl.col("value") != 0.0))
        for src, event_type in EVENT_COLUMNS.items()
        if src in frame.columns
    ]
    events = (
        pl.concat(parts).select(list(EVENT_SCHEMA)) if parts else pl.DataFrame(schema=EVENT_SCHEMA)
    )
    return candles, events.unique(subset=["event_time_ms", "event_type"], keep="last")


def fetch_history(
    symbol: str,
    yahoo_interval: str,
    start_ms: int,
    end_ms: int,
    timeout_s: int,
    *,
    ticker_factory: TickerFactory | None = None,
) -> YahooBatch:
    """Un pedido ``Ticker(symbol).history`` sobre ``[start_ms, end_ms)`` (síncrono).

    Precios sin ajuste por dividendos con acciones corporativas (``auto_adjust=False``,
    ``actions=True``), sin ``prepost`` ni ``repair`` (No-Go del spec). Yahoo entrega el OHLC
    ya ajustado por splits aun con ``auto_adjust=False``; ``Adj Close`` añade dividendos.

    Raises:
        YahooTransientError: HTTP 429 (``YFRateLimitError``) o error de red/timeout.
        SymbolNotFoundError: Yahoo responde "No data found" o el símbolo no tiene zona horaria.
    """
    configure_yfinance()
    ticker = (ticker_factory or _real_ticker)(symbol)
    try:
        frame: pd.DataFrame = ticker.history(
            interval=yahoo_interval,
            start=start_ms // 1000,
            end=-(-end_ms // 1000),
            auto_adjust=False,
            actions=True,
            prepost=False,
            repair=False,
            timeout=timeout_s,
        )
    except YFRateLimitError as exc:
        msg = f"{symbol}: HTTP 429 rate limit de Yahoo"
        raise YahooTransientError(msg) from exc
    except YFTzMissingError as exc:
        raise SymbolNotFoundError(symbol, str(exc)) from exc
    except YFPricesMissingError as exc:
        if NO_DATA_MARKER in str(exc).lower():
            raise SymbolNotFoundError(symbol, str(exc)) from exc
        # Rango sin velas (mercado cerrado): ventana procesada sin operaciones (RF-6).
        logger.debug("yahoo_window_empty", symbol=symbol, interval=yahoo_interval, reason=str(exc))
        return YahooBatch.empty(_instrument_type(ticker))
    except OSError as exc:  # red/timeout (curl_cffi, TimeoutError, ConnectionError, ...)
        msg = f"{symbol}: error de red ({type(exc).__name__}): {exc}"
        raise YahooTransientError(msg) from exc

    candles, events = to_polars(frame)
    return YahooBatch(candles=candles, events=events, instrument_type=_instrument_type(ticker))


def default_fetcher() -> FetchFn:
    """*Fetcher* por defecto (Yahoo real). Los tests lo sustituyen por uno falso."""
    return fetch_history
