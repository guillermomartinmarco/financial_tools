"""Clientes HTTP duales: Binance Public Data (data.binance.vision) y REST API.

Ambos comparten la política de reintentos de RF-5 / Art. 14: backoff exponencial
con *jitter* configurable por ``FT_BINANCE_MAX_RETRIES`` / ``FT_BINANCE_BASE_BACKOFF_MS``
y sólo ante ``418``/``429``/``5xx`` o errores de red. Cada reintento se loguea con
``structlog`` (Art. 9).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx
import structlog
from finances_core.config import FinancesSettings
from finances_core.marketdata import retry as core_retry
from finances_core.schemas.market_data import TF_MS_MAP
from tenacity import AsyncRetrying
from tenacity.wait import wait_base

from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket

logger = structlog.get_logger(__name__)

#: Códigos HTTP que Binance usa para limitar tasa (429) o banear IP temporalmente (418).
RATE_LIMIT_STATUS = frozenset({418, 429})
SERVER_ERROR_MIN = 500
HTTP_OK = 200
HTTP_NOT_FOUND = 404
MAX_BACKOFF_S = core_retry.MAX_BACKOFF_S
CHUNK_SIZE = 64 * 1024
#: Peso de ``exchangeInfo`` en el bucket REST (límites oficiales de Binance).
EXCHANGE_INFO_WEIGHT = 20
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA256_PREFIX = "sha256:"

JsonRow = list[Any]


class ChecksumMismatchError(ValueError):
    """El SHA256 del archivo descargado no coincide con el publicado (RF-9)."""


class PublicDataNotFoundError(Exception):
    """El archivo mensual de Public Data no existe (404) en el momento de descargarlo."""


def default_transport() -> httpx.AsyncBaseTransport | None:
    """Transporte HTTP por defecto (``None`` = red real). Punto de inyección para tests."""
    return None


# ------------------------------------------------------------------ reintentos
def is_retryable(exc: BaseException) -> bool:
    """Reintentar sólo ante 418/429/5xx o errores de red/timeout (nunca ante otros 4xx)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in RATE_LIMIT_STATUS or code >= SERVER_ERROR_MIN
    return isinstance(exc, httpx.TransportError)


def build_wait(settings: FinancesSettings, *, jitter: bool = True) -> wait_base:
    """Backoff exponencial ``base * 2**(n-1)`` (+ jitter uniforme ``[0, base]``)."""
    return core_retry.build_wait(settings.BINANCE_BASE_BACKOFF_MS, jitter=jitter)


def _describe_http_error(exc: BaseException | None) -> dict[str, object]:
    status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    return {"status_code": status_code}


def build_retrying(settings: FinancesSettings, operation: str, url: str) -> AsyncRetrying:
    """Política de reintentos: 1 intento + ``FT_BINANCE_MAX_RETRIES`` reintentos."""
    return core_retry.build_retrying(
        core_retry.RetryPolicy(
            max_retries=settings.BINANCE_MAX_RETRIES,
            base_backoff_ms=settings.BINANCE_BASE_BACKOFF_MS,
        ),
        is_retryable,
        "binance_http_retry",
        log_fields={"operation": operation, "url": url},
        describe_error=_describe_http_error,
    )


# ------------------------------------------------------------------ checksums
def checksum_sha256(file_path: Path) -> str:
    """SHA256 de un archivo en formato del plan: ``"sha256:<hex>"``."""
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            sha256.update(chunk)
    return f"{_SHA256_PREFIX}{sha256.hexdigest()}"


def _bare_digest(checksum: str) -> str:
    value = checksum.strip()
    if value.lower().startswith(_SHA256_PREFIX):
        value = value[len(_SHA256_PREFIX) :]
    return value.lower()


def parse_checksum_file(text: str) -> str:
    """Parsea un ``.CHECKSUM`` de Binance (``"<hex>  <archivo>"``) a ``"sha256:<hex>"``."""
    parts = text.split()
    digest = parts[0] if parts else ""
    if not _SHA256_RE.match(digest):
        msg = f"Contenido .CHECKSUM inválido: {text[:80]!r}"
        raise ValueError(msg)
    return f"{_SHA256_PREFIX}{digest.lower()}"


def checksums_match(expected: str, actual: str) -> bool:
    """Compara checksums ignorando el prefijo ``sha256:`` y mayúsculas."""
    return _bare_digest(expected) == _bare_digest(actual)


def verify_checksum(path: Path, expected: str, source: str) -> None:
    """Lanza :class:`ChecksumMismatchError` si el SHA256 de ``path`` no es ``expected``."""
    actual = checksum_sha256(path)
    if not checksums_match(expected, actual):
        msg = f"Checksum mismatch en {source}: esperado {expected}, obtenido {actual}"
        raise ChecksumMismatchError(msg)


# --------------------------------------------------------------------- clientes
class _BaseClient:
    def __init__(
        self,
        rate_limiter: DualTokenBucket,
        settings: FinancesSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or FinancesSettings()
        self.rate_limiter = rate_limiter
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=self._transport or default_transport(),
            follow_redirects=True,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = f"{type(self).__name__} debe usarse como 'async with'"
            raise RuntimeError(msg)
        return self._client


class BinancePublicDataClient(_BaseClient):
    """Cliente para Binance Public Data (archivos .zip mensuales + .CHECKSUM)."""

    def build_monthly_url(
        self, symbol: str, timeframe: str, market_type: str, year: int, month: int
    ) -> str:
        """URL del .zip mensual (los settings ya incluyen ``.../data/{spot|futures/um}``)."""
        base = (
            self.settings.BINANCE_PUBLIC_DATA_BASE_URL_SPOT
            if market_type == "spot"
            else self.settings.BINANCE_PUBLIC_DATA_BASE_URL_FUTURES
        )
        sym = symbol.upper()
        return (
            f"{base}/monthly/klines/{sym}/{timeframe}/{sym}-{timeframe}-{year:04d}-{month:02d}.zip"
        )

    async def _request(self, method: str, url: str) -> httpx.Response:
        """Petición con rate limit + reintentos; 404 se devuelve sin reintentar."""
        async for attempt in build_retrying(self.settings, f"public_data_{method.lower()}", url):
            with attempt:
                await self.rate_limiter.acquire_public()
                response = await self.http.request(method, url)
                if response.status_code != HTTP_NOT_FOUND:
                    response.raise_for_status()
                return response
        raise AssertionError  # pragma: no cover - tenacity relanza (reraise=True)

    async def check_exists(
        self, symbol: str, timeframe: str, market_type: str, year: int, month: int
    ) -> tuple[bool, str | None]:
        """HEAD del .zip mensual. Retorna ``(existe, "sha256:<hex>" | None)``."""
        url = self.build_monthly_url(symbol, timeframe, market_type, year, month)
        try:
            response = await self._request("HEAD", url)
        except httpx.HTTPStatusError as exc:
            logger.warning("public_data_head_failed", url=url, status_code=exc.response.status_code)
            return False, None
        if response.status_code != HTTP_OK:
            return False, None
        return True, await self.fetch_checksum(url)

    async def fetch_checksum(self, url: str) -> str | None:
        """Descarga ``<url>.CHECKSUM`` y lo normaliza; ``None`` si no está publicado."""
        checksum_url = f"{url}.CHECKSUM"
        try:
            response = await self._request("GET", checksum_url)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "public_data_checksum_unavailable",
                url=checksum_url,
                status_code=exc.response.status_code,
            )
            return None
        if response.status_code != HTTP_OK:
            return None
        try:
            return parse_checksum_file(response.text)
        except ValueError as exc:
            logger.warning("public_data_checksum_invalid", url=checksum_url, error=str(exc))
            return None

    async def _stream_to_file(self, url: str, path: Path) -> None:
        async for attempt in build_retrying(self.settings, "public_data_download", url):
            with attempt:
                await self.rate_limiter.acquire_public()
                async with self.http.stream("GET", url) as response:
                    if response.status_code == HTTP_NOT_FOUND:
                        msg = f"Public Data no encontrado: {url}"
                        raise PublicDataNotFoundError(msg)
                    response.raise_for_status()
                    with path.open("wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                            f.write(chunk)

    async def download(self, url: str, expected_checksum: str | None = None) -> Path:
        """Descarga en streaming a un archivo temporal y valida el checksum si se conoce.

        Raises:
            PublicDataNotFoundError: el archivo ya no existe (404).
            ChecksumMismatchError: el SHA256 no coincide.
        """
        fd, name = tempfile.mkstemp(suffix=".zip", prefix="ft_binance_")
        os.close(fd)
        temp_path = Path(name)
        logger.info("public_data_download_start", url=url)
        try:
            await self._stream_to_file(url, temp_path)
            if expected_checksum:
                verify_checksum(temp_path, expected_checksum, url)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        logger.info("public_data_download_complete", url=url, size=temp_path.stat().st_size)
        return temp_path


class BinanceRestClient(_BaseClient):
    """Cliente para Binance REST API (Spot ``/api/v3`` y USDⓈ-M Futures ``/fapi/v1``)."""

    @property
    def base_url_spot(self) -> str:
        return self.settings.BINANCE_REST_BASE_URL_SPOT

    @property
    def base_url_futures(self) -> str:
        return self.settings.BINANCE_REST_BASE_URL_FUTURES

    def base_url(self, market_type: str) -> str:
        return self.base_url_spot if market_type == "spot" else self.base_url_futures

    @staticmethod
    def endpoint(market_type: str) -> str:
        return "/api/v3/klines" if market_type == "spot" else "/fapi/v1/klines"

    @staticmethod
    def exchange_info_endpoint(market_type: str) -> str:
        return "/api/v3/exchangeInfo" if market_type == "spot" else "/fapi/v1/exchangeInfo"

    async def _get_json(
        self, url: str, params: dict[str, str | int] | None = None, weight: int = 1
    ) -> Any:
        async for attempt in build_retrying(self.settings, "rest_get", url):
            with attempt:
                await self.rate_limiter.acquire_rest(weight)
                response = await self.http.get(url, params=params)
                response.raise_for_status()
                return response.json()
        raise AssertionError  # pragma: no cover - tenacity relanza (reraise=True)

    async def fetch_symbols(self, market_type: str) -> set[str]:
        """Símbolos listados en ``exchangeInfo`` (validación RF-1 paso 1 / RF-9)."""
        url = f"{self.base_url(market_type)}{self.exchange_info_endpoint(market_type)}"
        data = await self._get_json(url, weight=EXCHANGE_INFO_WEIGHT)
        return {str(s["symbol"]).upper() for s in data.get("symbols", [])}

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: str,
        market_type: str,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
    ) -> list[JsonRow]:
        """Obtiene klines ``[start_ms, end_ms]`` paginando de ``limit`` en ``limit``."""
        all_klines: list[JsonRow] = []
        current_start = start_ms
        interval_ms = TF_MS_MAP[timeframe]
        url = f"{self.base_url(market_type)}{self.endpoint(market_type)}"

        while current_start <= end_ms:
            params: dict[str, str | int] = {
                "symbol": symbol.upper(),
                "interval": timeframe,
                "startTime": current_start,
                "endTime": min(end_ms, current_start + limit * interval_ms),
                "limit": limit,
            }
            data: list[JsonRow] = await self._get_json(url, params)
            if not data:
                break
            all_klines.extend(data)
            current_start = int(data[-1][0]) + interval_ms
            if len(data) < limit:
                break

        return all_klines
