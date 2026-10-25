"""Pasos 1-6 del pipeline determinista (RF-1): validar, inventariar, gaps y plan."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog
import uuid6
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data import (
    TF_MS_MAP,
    DownloadPlan,
    DownloadPlanItem,
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

#: Mensaje exigido por RF-9 para peticiones de datos futuros.
FUTURE_DATA_MESSAGE = "Cannot download future data"


def current_ms() -> int:
    return int(time.time() * 1000)


def new_task_id() -> str:
    """``task_id`` UUID v7 (RF-6, Art. 18)."""
    return str(uuid6.uuid7())


def build_rate_limiter(settings: FinancesSettings) -> DualTokenBucket:
    """Token Buckets diferenciados por fuente (RF-5)."""
    return DualTokenBucket(
        settings.BINANCE_PUBLIC_DATA_RATE_CAPACITY,
        settings.BINANCE_PUBLIC_DATA_RATE_REFILL_RATE,
        settings.BINANCE_REST_RATE_CAPACITY,
        settings.BINANCE_REST_RATE_REFILL_RATE,
    )


def validate_request(request: DownloadRequest, now_ms: int | None = None) -> int:
    """RF-1 paso 1 (parte local, sin red). Retorna el ``end_ms`` efectivo.

    - **SI** ``start_ms > now_ms - 1 vela`` (para cualquier timeframe pedido)
      **ENTONCES** ``ValueError("Cannot download future data")`` (RF-9).
    - ``end_ms`` ausente o futuro se acota a ``now_ms``.
    - Timeframes y ``end_ms > start_ms`` ya los garantiza ``DownloadRequest``.
    """
    now = now_ms if now_ms is not None else current_ms()
    for tf in request.timeframes:
        if request.start_ms > now - TF_MS_MAP[tf.value]:
            msg = (
                f"{FUTURE_DATA_MESSAGE}: start_ms={request.start_ms} > now_ms - 1 vela "
                f"({tf.value}) = {now - TF_MS_MAP[tf.value]}"
            )
            raise ValueError(msg)

    end_ms = request.end_ms if request.end_ms is not None else now
    if end_ms > now:
        logger.warning("end_ms_in_future_clamped", end_ms=end_ms, now_ms=now)
        end_ms = now
    if end_ms < request.start_ms:
        msg = f"end_ms ({end_ms}) debe ser >= start_ms ({request.start_ms})"
        raise ValueError(msg)
    return end_ms


def plan_file_path(task_id: str) -> Path:
    """Ruta persistente ``<raíz>/share/plans/download_plan_{task_id}.json`` (RF-1 paso 6)."""
    return FinancesSettings().SHARE_DIR / "plans" / f"download_plan_{task_id}.json"


def load_plan(task_id: str) -> DownloadPlan | None:
    """Plan persistido de ``task_id`` o ``None`` si no existe."""
    path = plan_file_path(task_id)
    if not path.is_file():
        return None
    return DownloadPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _save_plan(plan: DownloadPlan) -> Path:
    path = plan_file_path(plan.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path


@dataclass(frozen=True)
class PlanOutcome:
    """Plan + metadatos de planificación necesarios para el progreso (RF-6)."""

    plan: DownloadPlan
    gaps_found: int = 0
    skipped_symbols: list[str] = field(default_factory=list)


async def build_download_plan(
    request: DownloadRequest,
    *,
    task_id: str | None = None,
    settings: FinancesSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    now_ms: int | None = None,
) -> PlanOutcome:
    """Orquesta los pasos 1-6 del pipeline y persiste el plan JSON."""
    settings = settings or FinancesSettings()
    task_id = task_id or new_task_id()
    end_ms = validate_request(request, now_ms)
    market = request.market_type.value
    rate_limiter = build_rate_limiter(settings)

    items: list[DownloadPlanItem] = []
    gaps_found = 0
    skipped: list[str] = []

    async with (
        BinancePublicDataClient(
            rate_limiter, settings=settings, transport=transport
        ) as public_client,
        BinanceRestClient(rate_limiter, settings=settings, transport=transport) as rest_client,
    ):
        # Paso 1 (red): símbolos en exchangeInfo. RF-9: desconocido -> warning y se omite.
        listed = await rest_client.fetch_symbols(market)
        symbols: list[str] = []
        for raw in request.symbols:
            symbol = raw.upper()
            if symbol in listed:
                symbols.append(symbol)
            else:
                logger.warning("symbol_not_in_exchange_info", symbol=symbol, market=market)
                skipped.append(symbol)

        for symbol in symbols:
            for tf_enum in request.timeframes:
                tf = tf_enum.value
                tf_ms = TF_MS_MAP[tf]
                log = logger.bind(symbol=symbol, timeframe=tf, market=market)

                # Paso 2: inventario local (I/O Polars fuera del event loop, Art. 3)
                local = await asyncio.to_thread(
                    inventory_local_parquet, settings.PARQUET_DIR, symbol, tf, market
                )
                # Cada partición cubre hasta el cierre de su última vela.
                covered = [(lo, hi + tf_ms) for lo, hi in local]

                # Paso 3: gaps
                gaps = compute_gaps(request.start_ms, end_ms, covered)
                gaps_found += len(gaps)
                log.info("gaps_computed", gaps=len(gaps), local_partitions=len(local))
                if not gaps:
                    continue

                # Paso 4: Public Data (mes a mes)
                public_items, uncovered = await check_public_data_availability(
                    gaps, symbol, tf, market, public_client
                )
                # Paso 5: fallback REST para cada sub-rango no publicado
                rest_items = plan_rest_fallback(
                    uncovered, symbol, tf, market, request.limit_per_request, rest_client
                )
                log.info(
                    "plan_pair_ready", public_items=len(public_items), rest_items=len(rest_items)
                )
                items.extend(public_items)
                items.extend(rest_items)

    plan = DownloadPlan(task_id=task_id, created_at_ms=current_ms(), items=items)
    # Paso 6: persistir plan
    path = await asyncio.to_thread(_save_plan, plan)
    logger.info("plan_saved", task_id=task_id, path=str(path), items=len(items))
    return PlanOutcome(plan=plan, gaps_found=gaps_found, skipped_symbols=skipped)


async def generate_download_plan(
    request: DownloadRequest,
    *,
    task_id: str | None = None,
    settings: FinancesSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DownloadPlan:
    """Pasos 1-6 del pipeline; retorna sólo el :class:`DownloadPlan`."""
    outcome = await build_download_plan(
        request, task_id=task_id, settings=settings, transport=transport
    )
    return outcome.plan
