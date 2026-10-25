"""Fixtures de ``ft_market_data_binance``: Binance simulado vía ``httpx.MockTransport``.

Ningún test toca la red: todas las peticiones HTTP las responde :class:`FakeBinance`.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import structlog
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data import TF_MS_MAP

from finances_downloader.ft_market_data_binance import client as client_module

PUBLIC_HOST = "data.binance.vision"
SPOT_REST_HOST = "api.binance.com"
FUTURES_REST_HOST = "fapi.binance.com"

#: Microsegundos: Binance Spot Public Data los usa desde 2025-01-01.
US_FACTOR = 1000


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def task_id_from(output: str) -> str:
    """Extrae el primer UUID impreso por la CLI (tabla de resultado)."""
    match = _UUID_RE.search(output)
    assert match is not None, output
    return match.group(0)


def ms(year: int, month: int, day: int = 1, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def kline_row(open_ms: int, tf: str = "1h", price: float = 100.0) -> list[str | int]:
    """Vela en formato REST de Binance (12 campos)."""
    close_ms = open_ms + TF_MS_MAP[tf] - 1
    return [
        open_ms,
        f"{price:.8f}",
        f"{price + 1:.8f}",
        f"{price - 1:.8f}",
        f"{price + 0.5:.8f}",
        "10.00000000",
        close_ms,
        "1000.00000000",
        7,
        "4.00000000",
        "400.00000000",
        "0",
    ]


def csv_bytes(
    open_times: list[int],
    tf: str = "1h",
    *,
    header: bool = False,
    microseconds: bool = False,
    price: float = 100.0,
) -> bytes:
    """CSV de Public Data. Spot no trae cabecera; Futures sí."""
    lines: list[str] = []
    if header:
        lines.append(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore"
        )
    for open_ms in open_times:
        row = kline_row(open_ms, tf, price)
        if microseconds:
            row[0] = int(row[0]) * US_FACTOR
            row[6] = int(row[6]) * US_FACTOR + 999
        lines.append(",".join(str(v) for v in row))
    return ("\n".join(lines) + "\n").encode()


def zip_bytes(csv_name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(csv_name, payload)
    return buffer.getvalue()


def month_open_times(year: int, month: int, tf: str = "1h") -> list[int]:
    start = ms(year, month)
    end = ms(year + 1, 1) if month == 12 else ms(year, month + 1)  # noqa: PLR2004
    return list(range(start, end, TF_MS_MAP[tf]))


@dataclass
class FakeBinance:
    """Servidor Binance en memoria (Public Data + REST + exchangeInfo)."""

    symbols: set[str] = field(default_factory=lambda: {"BTCUSDT", "ETHUSDT"})
    #: (market, symbol, tf, year, month) -> bytes del .zip mensual
    monthly_zips: dict[tuple[str, str, str, int, int], bytes] = field(default_factory=dict)
    #: Si False, no se publica el archivo .CHECKSUM.
    publish_checksums: bool = True
    #: Sobrescribe el checksum publicado (para simular corrupción).
    checksum_override: dict[str, str] = field(default_factory=dict)
    #: Rango [desde, hasta) en el que REST tiene velas.
    rest_from_ms: int = 0
    rest_to_ms: int = 2**62
    #: Respuestas forzadas por prefijo de ruta: se consumen en orden.
    forced_status: dict[str, list[int]] = field(default_factory=dict)
    #: Excepción de red forzada por prefijo de ruta: se consumen en orden.
    forced_errors: dict[str, list[Exception]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    # ------------------------------------------------------------------ setup
    def add_month(
        self,
        market: str,
        symbol: str,
        tf: str,
        year: int,
        month: int,
        *,
        header: bool = False,
        microseconds: bool = False,
        price: float = 100.0,
    ) -> bytes:
        name = f"{symbol}-{tf}-{year:04d}-{month:02d}.csv"
        payload = zip_bytes(
            name,
            csv_bytes(
                month_open_times(year, month, tf),
                tf,
                header=header,
                microseconds=microseconds,
                price=price,
            ),
        )
        self.monthly_zips[(market, symbol, tf, year, month)] = payload
        return payload

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, method: str, path_fragment: str) -> int:
        return sum(1 for r in self.requests if r.method == method and path_fragment in r.url.path)

    # ---------------------------------------------------------------- handler
    def _forced(self, path: str) -> int | None:
        for prefix, statuses in self.forced_status.items():
            if path.startswith(prefix) and statuses:
                return statuses.pop(0)
        for prefix, errors in self.forced_errors.items():
            if path.startswith(prefix) and errors:
                raise errors.pop(0)
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        forced = self._forced(request.url.path)
        if forced is not None:
            return httpx.Response(forced, json={"code": -1, "msg": "forced"})
        host = request.url.host
        if host == PUBLIC_HOST:
            return self._public(request)
        if host in {SPOT_REST_HOST, FUTURES_REST_HOST}:
            return self._rest(request)
        return httpx.Response(404)

    def _public(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        market = "futures" if path.startswith("/data/futures/um/") else "spot"
        filename = path.rsplit("/", 1)[-1]
        is_checksum = filename.endswith(".CHECKSUM")
        zip_name = filename.removesuffix(".CHECKSUM")
        stem = zip_name.removesuffix(".zip")
        try:
            symbol, tf, year, month = stem.split("-")
            key = (market, symbol, tf, int(year), int(month))
        except ValueError:
            return httpx.Response(404)
        payload = self.monthly_zips.get(key)
        if payload is None:
            return httpx.Response(404)
        if is_checksum:
            if not self.publish_checksums:
                return httpx.Response(404)
            digest = self.checksum_override.get(zip_name, hashlib.sha256(payload).hexdigest())
            return httpx.Response(200, text=f"{digest}  {zip_name}\n")
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload))})
        return httpx.Response(200, content=payload)

    def _rest(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/exchangeInfo"):
            return httpx.Response(
                200,
                json={
                    "symbols": [{"symbol": s, "status": "TRADING"} for s in sorted(self.symbols)]
                },
            )
        if not path.endswith("/klines"):
            return httpx.Response(404)
        params = request.url.params
        symbol = params["symbol"]
        if symbol not in self.symbols:
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        tf = params["interval"]
        step = TF_MS_MAP[tf]
        start = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params.get("limit", "500"))
        first = max(start, self.rest_from_ms)
        first = first + (-first % step)  # alinear al timeframe
        rows: list[list[str | int]] = []
        t = first
        while t <= end and t < self.rest_to_ms and len(rows) < limit:
            rows.append(kline_row(t, tf, price=200.0))
            t += step
        return httpx.Response(200, json=rows)


@pytest.fixture
def fake_binance() -> FakeBinance:
    return FakeBinance()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FinancesSettings:
    """Settings aislados en ``tmp_path`` y sin esperas de backoff."""
    monkeypatch.setenv("FT_BINANCE_BASE_BACKOFF_MS", "0")
    return FinancesSettings()


@pytest.fixture(autouse=True)
def _isolate_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Los planes ``download_plan_{task_id}.json`` se escriben en un temp aislado."""
    plan_tmp = tmp_path / "systemp"
    plan_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(plan_tmp))
    yield


@pytest.fixture
def use_fake_transport(
    monkeypatch: pytest.MonkeyPatch, fake_binance: FakeBinance
) -> Callable[[], httpx.AsyncBaseTransport]:
    """Inyecta el transporte simulado como transporte por defecto de los clientes."""

    def factory() -> httpx.AsyncBaseTransport:
        return fake_binance.transport

    monkeypatch.setattr(client_module, "default_transport", factory)
    monkeypatch.setenv("FT_BINANCE_BASE_BACKOFF_MS", "0")
    return factory


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """La CLI reconfigura logging sobre streams de ``CliRunner``: se restaura tras cada test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)
