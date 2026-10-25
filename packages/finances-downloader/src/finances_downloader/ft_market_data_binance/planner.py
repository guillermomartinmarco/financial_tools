import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog
import uuid6
from finances_core.config import settings
from finances_core.schemas.market_data import (
    TF_MS_MAP,
    DownloadPlan,
    DownloadRequest,
)

from finances_downloader.ft_market_data_binance.client import (
    BinancePublicDataClient,
    BinanceRestClient,
)
from finances_downloader.ft_market_data_binance.inventory import (
    compute_gaps,
    inventory_local_parquet,
)
from finances_downloader.ft_market_data_binance.public_data import check_public_data_availability
from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket
from finances_downloader.ft_market_data_binance.rest_api import plan_rest_fallback

logger = structlog.get_logger(__name__)


def parse_time_input(s: str) -> int:
    """
    Parsea entrada de tiempo flexible a ms UTC.
    Acepta: "2024-01-01", "2024-01-01T00:00:00Z", "1704067200000", "now"
    """
    s = s.strip()
    if s.lower() == "now":
        return int(time.time() * 1000)

    # Intentar como timestamp ms (número grande)
    if s.isdigit() and len(s) >= 10:
        return int(s)

    # Intentar como ISO 8601
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass

    # Intentar formatos comunes
    for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"]:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=UTC)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue

    raise ValueError(f"Formato de tiempo no reconocido: {s}. Use ISO 8601, ms epoch, o 'now'")


def validate_request(request: DownloadRequest) -> None:
    """Valida la request completa antes de proceder."""
    now_ms = int(time.time() * 1000)

    if request.start_ms > now_ms:
        raise ValueError(f"start_ms ({request.start_ms}) no puede ser en el futuro")

    if request.end_ms and request.end_ms > now_ms:
        logger.warning("end_ms en el futuro, ajustando a ahora", end_ms=request.end_ms, now_ms=now_ms)
        request.end_ms = now_ms

    # Validar timeframes válidos
    valid_tfs = set(TF_MS_MAP.keys())
    for tf in request.timeframes:
        if tf.value not in valid_tfs:
            raise ValueError(f"Timeframe inválido: {tf}. Válidos: {sorted(valid_tfs)}")

    # Validar símbolos (básico - validación real via exchangeInfo sería en paso 1 completo)
    for symbol in request.symbols:
        if not symbol or len(symbol) < 6:
            raise ValueError(f"Símbolo inválido: {symbol}")


async def generate_download_plan(request: DownloadRequest) -> DownloadPlan:
    """
    Orquesta pasos 1-6 del pipeline determinista:
    1. Validar petición
    2. Inventario local
    3. Determinar gaps
    4. Verificar Public Data
    5. Planificar REST fallback
    6. Generar plan JSON
    """
    task_id = str(uuid6.uuid7())
    created_at_ms = int(time.time() * 1000)

    validate_request(request)

    end_ms = request.end_ms or int(time.time() * 1000)

    all_items = []

    # Create rate limiter from settings
    rate_limiter = DualTokenBucket(
        settings.BINANCE_PUBLIC_DATA_RATE_CAPACITY,
        settings.BINANCE_PUBLIC_DATA_RATE_REFILL_RATE,
        settings.BINANCE_REST_RATE_CAPACITY,
        settings.BINANCE_REST_RATE_REFILL_RATE,
    )

    async with BinancePublicDataClient(rate_limiter) as public_client, \
               BinanceRestClient(rate_limiter) as rest_client:

        for symbol in request.symbols:
            for tf_enum in request.timeframes:
                tf = tf_enum.value
                market = request.market_type.value

                logger.info("planning_symbol_tf", symbol=symbol, timeframe=tf, market=market)

                # Paso 2: Inventario local
                local_ranges = inventory_local_parquet(
                    settings.PARQUET_DIR, symbol, tf, market
                )

                # Paso 3: Gaps
                gaps = compute_gaps(request.start_ms, end_ms, local_ranges)
                logger.info("gaps_computed", symbol=symbol, tf=tf, gaps_count=len(gaps))

                if not gaps:
                    continue

                # Paso 4: Public Data
                public_items, uncovered = await check_public_data_availability(
                    gaps, symbol, tf, market, public_client
                )
                logger.info("public_data_checked", symbol=symbol, tf=tf,
                          public_items=len(public_items), uncovered_gaps=len(uncovered))

                # Paso 5: REST fallback
                rest_items = plan_rest_fallback(
                    uncovered, symbol, tf, market, request.limit_per_request, rest_client
                )
                logger.info("rest_fallback_planned", symbol=symbol, tf=tf, rest_items=len(rest_items))

                all_items.extend(public_items)
                all_items.extend(rest_items)

    plan = DownloadPlan(
        task_id=task_id,
        created_at_ms=created_at_ms,
        items=all_items,
    )

    # Paso 6: Persistir plan a archivo temporal
    plan_file = Path(tempfile.gettempdir()) / f"download_plan_{task_id}.json"
    plan_file.write_text(plan.model_dump_json(indent=2))
    logger.info("plan_saved", path=str(plan_file), items=len(all_items))

    return plan
