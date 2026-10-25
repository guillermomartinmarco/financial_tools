"""Pasos 1-5 de RF-1: validar, inventariar, calcular gaps, aplicar RF-3 y persistir el plan.

La planificación es **pura** (Art. 18): sólo lee el Parquet local, nunca la red.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from finances_core.config import FinancesSettings
from finances_core.marketdata import plans as core_plans
from finances_core.marketdata.inventory import (
    ANY_MARKET_TYPE,
    compute_gaps,
    coverage_ranges,
    inventory_local_parquet,
)
from finances_core.marketdata.plans import current_ms, new_task_id
from finances_core.schemas.market_data import TF_MS_MAP
from finances_core.schemas.market_data_yfinance import (
    YFDownloadPlan,
    YFDownloadRequest,
    YFPlanItem,
    YFUnfillableGap,
)

from finances_downloader.ft_market_data_yfinance.timeframes import (
    YAHOO_INTERVAL,
    min_candle_ms,
    split_by_limits,
)

logger = structlog.get_logger(__name__)

#: Directorio ``{FT_PARQUET_DIR}/{EXCHANGE}/`` de las velas de Yahoo.
EXCHANGE = "yfinance"

__all__ = [
    "EXCHANGE",
    "PlanOutcome",
    "build_download_plan",
    "load_plan",
    "new_task_id",
    "request_symbols",
    "validate_request",
]


def request_symbols(request: YFDownloadRequest) -> list[str]:
    """Símbolos tal cual Yahoo (``AAPL``, ``^GSPC``, ``EURUSD=X``), sin duplicados."""
    return list(dict.fromkeys(s.strip() for s in request.symbols if s.strip()))


def validate_request(request: YFDownloadRequest, now_ms: int | None = None) -> int:
    """RF-1 paso 1 (sin red). Retorna el ``end_ms`` efectivo (``None``/futuro -> ``now``).

    Los timeframes de RF-2 y ``symbols`` 1..100 ya los garantiza ``YFDownloadRequest``.

    Raises:
        ValueError: ``start_ms`` futuro (``"Cannot download future data"``),
            ``end_ms < start_ms`` o sin símbolos.
    """
    if not request_symbols(request):
        msg = "symbols no puede estar vacío"
        raise ValueError(msg)
    timeframes_ms: dict[str, int] = {tf: TF_MS_MAP[tf] for tf in request.timeframes}
    return core_plans.validate_time_range(request.start_ms, request.end_ms, timeframes_ms, now_ms)


def load_plan(task_id: str, settings: FinancesSettings | None = None) -> YFDownloadPlan | None:
    """Plan persistido de ``task_id`` (``share/plans/``) o ``None`` si no existe."""
    return core_plans.load_plan(task_id, YFDownloadPlan, settings)


def _relevant_gaps(
    gaps: list[tuple[int, int]], end_ms: int, timeframe: str
) -> list[tuple[int, int]]:
    """Descarta gaps acotados por una vela local posterior y más cortos que una vela.

    Si el gap termina en la primera vela de otra partición (``hi < end_ms``) y dura
    menos que la vela más corta posible, ninguna vela puede abrir dentro de él
    (p. ej. el resto de hora entre ``última + 23 h`` y la medianoche siguiente en un
    cambio de mes): pedirlo sólo gastaría un pedido a Yahoo.
    """
    span = min_candle_ms(timeframe)
    return [(lo, hi) for lo, hi in gaps if hi >= end_ms or hi - lo >= span]


@dataclass(frozen=True)
class PlanOutcome:
    """Plan + número de gaps detectados (progreso RF-8)."""

    plan: YFDownloadPlan
    gaps_found: int = 0


def build_download_plan(
    request: YFDownloadRequest,
    *,
    task_id: str | None = None,
    settings: FinancesSettings | None = None,
    now_ms: int | None = None,
) -> PlanOutcome:
    """Genera y persiste el plan determinista (síncrono: el servicio lo lanza en un hilo)."""
    settings = settings or FinancesSettings()
    task_id = task_id or new_task_id()
    now = now_ms if now_ms is not None else current_ms()
    end_ms = validate_request(request, now)

    items: list[YFPlanItem] = []
    unfillable: list[YFUnfillableGap] = []
    gaps_found = 0
    for symbol in sorted(request_symbols(request)):
        for tf in dict.fromkeys(request.timeframes):
            local = inventory_local_parquet(
                settings.PARQUET_DIR, symbol, tf, ANY_MARKET_TYPE, exchange=EXCHANGE
            )
            gaps = _relevant_gaps(
                compute_gaps(request.start_ms, end_ms, coverage_ranges(local, min_candle_ms(tf))),
                end_ms,
                tf,
            )
            gaps_found += len(gaps)
            for gap in gaps:
                windows, beyond = split_by_limits(gap, tf, now)
                items.extend(
                    YFPlanItem(
                        symbol=symbol,
                        timeframe=tf,
                        yahoo_interval=YAHOO_INTERVAL[tf],
                        start_ms=lo,
                        end_ms=hi,
                    )
                    for lo, hi in windows
                )
                unfillable.extend(
                    YFUnfillableGap(
                        symbol=symbol,
                        timeframe=tf,
                        start_ms=lo,
                        end_ms=hi,
                        reason="beyond_yahoo_lookback",
                    )
                    for lo, hi in beyond
                )
            logger.debug(
                "pair_planned", symbol=symbol, timeframe=tf, partitions=len(local), gaps=len(gaps)
            )

    plan = YFDownloadPlan(
        task_id=task_id, created_at_ms=now, items=items, gaps_unfillable=unfillable
    )
    path = core_plans.save_plan(task_id, plan, settings)
    logger.info(
        "download_plan_saved",
        path=str(path),
        items=len(items),
        gaps_found=gaps_found,
        unfillable=len(unfillable),
    )
    return PlanOutcome(plan=plan, gaps_found=gaps_found)
