import hashlib
import tempfile
from pathlib import Path

import httpx
import structlog
from finances_core.config import settings
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket

logger = structlog.get_logger(__name__)


class BinancePublicDataClient:
    """Cliente para Binance Public Data (data.binance.vision)."""

    def __init__(self, rate_limiter: DualTokenBucket):
        self.base_url_spot = settings.BINANCE_PUBLIC_DATA_BASE_URL_SPOT
        self.base_url_futures = settings.BINANCE_PUBLIC_DATA_BASE_URL_FUTURES
        self.rate_limiter = rate_limiter
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "BinancePublicDataClient":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._client:
            await self._client.aclose()

    def _build_monthly_url(self, symbol: str, timeframe: str, market_type: str, year: int, month: int) -> str:
        """Construye URL para archivo mensual de klines.

        Los settings ya incluyen el path base completo (ej. .../data/spot),
        así que solo añadimos monthly/klines/{SYMBOL}/{TF}/...
        """
        base = self.base_url_spot if market_type == "spot" else self.base_url_futures
        return f"{base}/monthly/klines/{symbol.upper()}/{timeframe}/{symbol.upper()}-{timeframe}-{year:04d}-{month:02d}.zip"

    async def check_exists(self, symbol: str, timeframe: str, market_type: str, year: int, month: int) -> tuple[bool, str | None]:
        """Verifica si existe archivo mensual via HEAD. Retorna (existe, checksum_sha256_si_disponible)."""
        await self.rate_limiter.acquire_public()
        url = self._build_monthly_url(symbol, timeframe, market_type, year, month)
        client = self._client
        assert client is not None
        try:
            response = await client.head(url, follow_redirects=True)
            if response.status_code == 200:
                # Intentar obtener checksum real: x-amz-meta-sha256 (S3) o archivo .CHECKSUM
                checksum = response.headers.get("x-amz-meta-sha256")
                if not checksum:
                    # ETag no es SHA256 fiable; no usarlo
                    checksum = None
                return True, checksum
            return False, None
        except httpx.RequestError as e:
            logger.warning("public_data_head_failed", url=url, error=str(e))
            return False, None

    async def download_monthly(self, symbol: str, timeframe: str, market_type: str, year: int, month: int, expected_checksum: str | None = None) -> Path:
        """Descarga archivo .zip mensual a archivo temporal. Valida checksum si se proporciona."""
        await self.rate_limiter.acquire_public()
        url = self._build_monthly_url(symbol, timeframe, market_type, year, month)
        logger.info("public_data_download_start", url=url, symbol=symbol, timeframe=timeframe)

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        temp_path = Path(temp_file.name)
        temp_file.close()

        try:
            client = self._client
            assert client is not None
            async with client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                with open(temp_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)

            if expected_checksum:
                actual = checksum_sha256(temp_path)
                if actual != expected_checksum:
                    raise ValueError(f"Checksum mismatch: expected {expected_checksum}, got {actual}")

            logger.info("public_data_download_complete", path=str(temp_path), size=temp_path.stat().st_size)
            return temp_path

        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise


class BinanceRestClient:
    """Cliente para Binance REST API (Spot y Futures)."""

    def __init__(self, rate_limiter: DualTokenBucket):
        self.base_url_spot = settings.BINANCE_REST_BASE_URL_SPOT
        self.base_url_futures = settings.BINANCE_REST_BASE_URL_FUTURES
        self.rate_limiter = rate_limiter
        self.max_retries = settings.BINANCE_MAX_RETRIES
        self.base_backoff = settings.BINANCE_BASE_BACKOFF_MS / 1000.0
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "BinanceRestClient":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._client:
            await self._client.aclose()

    def _get_base_url(self, market_type: str) -> str:
        return self.base_url_spot if market_type == "spot" else self.base_url_futures

    def _get_endpoint(self, market_type: str) -> str:
        return "/api/v3/klines" if market_type == "spot" else "/fapi/v1/klines"

    @retry(
        wait=wait_exponential_jitter(initial=1, max=60),
        stop=stop_after_attempt(3),  # se sobrescribe en __init__ si settings disponible
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
        reraise=True,
    )
    async def _request_with_retry(self, method: str, url: str, params: dict[str, str | int | float | bool | None]) -> httpx.Response:
        await self.rate_limiter.acquire_rest()
        client = self._client
        assert client is not None
        response = await client.request(method, url, params=params)
        if response.status_code in (429, 418):
            # Rate limited - tenacity reintentará
            raise httpx.HTTPStatusError(f"Rate limited: {response.status_code}", request=response.request, response=response)
        response.raise_for_status()
        return response

    def _configure_retry(self) -> None:
        """Configura reintentos usando settings (llamado tras inicializar settings)."""
        # tenacity no permite cambiar decorador en runtime fácilmente;
        # los valores por defecto (3 retries, base 1s) coinciden con settings por defecto.
        pass

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: str,
        market_type: str,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
    ) -> list[list[str | int | float]]:
        """Obtiene klines via REST API con paginación automática."""
        all_klines = []
        current_start = start_ms
        interval_ms = TF_MS_MAP.get(timeframe, 60_000)

        while current_start < end_ms:
            params = {
                "symbol": symbol.upper(),
                "interval": timeframe,
                "startTime": current_start,
                "endTime": min(end_ms, current_start + limit * interval_ms),
                "limit": limit,
            }
            url = f"{self._get_base_url(market_type)}{self._get_endpoint(market_type)}"

            response = await self._request_with_retry("GET", url, params)
            data = response.json()

            if not data:
                break

            all_klines.extend(data)
            # Siguiente página empieza después del último open_time
            last_open_time = data[-1][0]
            current_start = last_open_time + interval_ms

            if len(data) < limit:
                break

        return all_klines


def checksum_sha256(file_path: Path) -> str:
    """Calcula SHA256 de un archivo."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return f"sha256:{sha256.hexdigest()}"


# Import TF_MS_MAP for use in rest client
from finances_core.schemas.market_data import TF_MS_MAP
