"""Capa de servicio compartida por CLI y API (Art. 1): orquestación y estado de tareas.

- :func:`create_download_task` registra la tarea (``PENDING``) en el despacho, antes
  de planificar, para que ``status`` nunca devuelva 404 para una tarea despachada.
- :func:`run_download` ejecuta ``PLANNING`` (pasos 1-6) y :func:`execute_download_plan`
  (paso 7). Las fases avanzan de forma monótona (RF-6).
- El I/O bloqueante (Polars, zip, SQLite) se delega a hilos (Art. 3).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import polars as pl
import structlog
from finances_core.config import FinancesSettings
from finances_core.marketdata import tasks_lifecycle as core_tasks
from finances_core.marketdata.tasks_lifecycle import (
    MAX_CONCURRENT_ENV,
    TERMINAL_STATUSES,
    PhaseCallback,
    TaskNotFinishedError,
    TaskRecorder,
    ensure_finished,
    json_field,
    load_task_row,
    resolve_max_concurrent,
)
from finances_core.schemas.market_data import (
    DownloadPlan,
    DownloadPlanItem,
    DownloadProgress,
    DownloadRequest,
    DownloadResult,
)

from finances_downloader.ft_market_data_binance.client import (
    BinancePublicDataClient,
    BinanceRestClient,
    ChecksumMismatchError,
    PublicDataNotFoundError,
)
from finances_downloader.ft_market_data_binance.normalizer import (
    normalize_rest_json,
    read_public_data_zip,
)
from finances_downloader.ft_market_data_binance.planner import (
    build_download_plan,
    build_rate_limiter,
    new_task_id,
)
from finances_downloader.ft_market_data_binance.rest_api import plan_rest_fallback
from finances_downloader.ft_market_data_binance.writer import write_parquet_partitioned

logger = structlog.get_logger(__name__)

__all__ = [
    "MAX_CONCURRENT_ENV",
    "PHASE_ORDER",
    "TERMINAL_STATUSES",
    "TaskNotFinishedError",
    "create_download_task",
    "execute_download_plan",
    "get_progress",
    "get_result",
    "new_task_id",
    "resolve_max_concurrent",
    "run_download",
    "run_download_sync",
]

#: Orden de fases de RF-6. Una tarea nunca retrocede en esta secuencia.
PHASE_ORDER = (
    "PENDING",
    "PLANNING",
    "DOWNLOADING_PUBLIC",
    "DOWNLOADING_REST",
    "NORMALIZING",
    "WRITING_PARQUET",
    "COMPLETED",
    "FAILED",
)
#: Reintentos adicionales ante checksum incorrecto antes del fallback REST (RF-9).
CHECKSUM_RETRIES = 1
REST_FALLBACK_LIMIT = 1000

Pair = tuple[str, str, str]  # (symbol, timeframe, market_type)


def _pair_key(pair: Pair) -> str:
    return "/".join(pair)


def _request_pairs(request: DownloadRequest) -> list[list[str]]:
    return [[s.upper(), tf.value] for s in request.symbols for tf in request.timeframes]


def create_download_task(
    task_id: str, request: DownloadRequest, *, settings: FinancesSettings | None = None
) -> None:
    """Registra la tarea en ``PENDING`` (idempotente). Se llama en el despacho."""
    core_tasks.create_download_task(
        task_id,
        {
            "request": request.model_dump(mode="json"),
            "pairs_total": _request_pairs(request),
            "pairs_done": [],
            "gaps_unfillable": [],
        },
        settings=settings,
    )


# ---------------------------------------------------------------- estado
class _TaskRecorder(TaskRecorder):
    """``TaskRecorder`` del núcleo con el orden de fases de RF-6 y resultado Binance."""

    @classmethod
    async def open_binance(
        cls, task_id: str, settings: FinancesSettings, on_phase: PhaseCallback | None
    ) -> _TaskRecorder:
        return await cls.open(task_id, settings, on_phase, phase_order=PHASE_ORDER)

    async def fail(self, message: str) -> DownloadResult:
        """Marca ``FAILED`` con error detallado (RF-9) y devuelve el resultado."""
        await self.mark_failed(message)
        return DownloadResult(
            task_id=self.task_id,
            status="FAILED",
            parquet_paths=[],
            stats={},
            errors=list(self.errors),
            gaps_unfillable=list(self.summary.get("gaps_unfillable", [])),
        )


@dataclass
class _RunState:
    frames: dict[Pair, list[pl.DataFrame]] = field(default_factory=lambda: defaultdict(list))
    stats: dict[str, dict[str, int]] = field(default_factory=dict)
    unfillable: list[dict[str, object]] = field(default_factory=list)
    fallback: list[DownloadPlanItem] = field(default_factory=list)
    hard_errors: list[str] = field(default_factory=list)
    filled_public: int = 0
    filled_rest: int = 0

    def pair_stats(self, pair: Pair) -> dict[str, int]:
        return self.stats.setdefault(
            _pair_key(pair), {"candles": 0, "bytes": 0, "public": 0, "rest": 0}
        )

    def totals(self) -> dict[str, int]:
        return {
            "processed_candles": sum(s["candles"] for s in self.stats.values()),
            "downloaded_bytes": sum(s["bytes"] for s in self.stats.values()),
            "gaps_filled_public": self.filled_public,
            "gaps_filled_rest": self.filled_rest,
        }


def _pair(item: DownloadPlanItem) -> Pair:
    return (item.symbol, item.timeframe, item.market_type)


def _combine(frames: list[pl.DataFrame]) -> pl.DataFrame:
    return (
        pl.concat(frames, how="vertical")
        .unique(subset=["open_time_ms"], keep="last", maintain_order=True)
        .sort("open_time_ms")
    )


# ----------------------------------------------------------- orquestador
async def execute_download_plan(
    plan: DownloadPlan,
    max_concurrent: int | None = None,
    *,
    settings: FinancesSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    on_phase: PhaseCallback | None = None,
    _recorder: _TaskRecorder | None = None,
) -> DownloadResult:
    """Paso 7: descarga (semáforo RF-2), valida, normaliza (RF-3) y escribe (RF-4).

    Fases: ``DOWNLOADING_PUBLIC`` → ``DOWNLOADING_REST`` (incluye fallbacks por
    checksum/404) → ``NORMALIZING`` → ``WRITING_PARQUET`` → ``COMPLETED``/``FAILED``.
    Un item fallido no detiene al resto; los datos escritos persisten.
    """
    settings = settings or FinancesSettings()
    rec = _recorder or await _TaskRecorder.open_binance(plan.task_id, settings, on_phase)
    with structlog.contextvars.bound_contextvars(task_id=plan.task_id):
        try:
            concurrency = resolve_max_concurrent(max_concurrent, settings)
            return await _PlanExecutor(plan, concurrency, settings, rec).run(transport)
        except Exception as exc:
            logger.exception("download_execution_failed", error=str(exc))
            await rec.fail(f"Error inesperado ({type(exc).__name__}): {exc}")
            raise


class _PlanExecutor:
    """Estado y pasos de una ejecución del paso 7 (un objeto por plan)."""

    def __init__(
        self,
        plan: DownloadPlan,
        max_concurrent: int,
        settings: FinancesSettings,
        rec: _TaskRecorder,
    ) -> None:
        self.plan = plan
        self.settings = settings
        self.rec = rec
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.state = _RunState()

    # --------------------------------------------------------- registro
    async def hard_error(self, message: str) -> None:
        logger.error("download_item_failed", error=message)
        self.state.hard_errors.append(message)
        await self.rec.add_error(message)

    async def unfillable(self, item: DownloadPlanItem) -> None:
        """RF-9: gap sin datos en Binance -> warning + ``gaps_unfillable`` y continuar."""
        lo, hi = item.covers_range_ms
        entry: dict[str, object] = {
            "symbol": item.symbol,
            "timeframe": item.timeframe,
            "market_type": item.market_type,
            "range_ms": [lo, hi],
            "reason": "Sin datos en Binance Public Data ni REST",
        }
        logger.warning("gap_unfillable", **entry)
        self.state.unfillable.append(entry)
        self.rec.summary["gaps_unfillable"] = self.state.unfillable
        pair = _pair_key(_pair(item))
        await self.rec.add_error(f"Gap sin datos en Binance {pair} [{lo}, {hi}]")

    async def collect(
        self,
        item: DownloadPlanItem,
        df: pl.DataFrame,
        source: Literal["public", "rest"],
        size: int,
    ) -> None:
        pair = _pair(item)
        self.state.frames[pair].append(df)
        stats = self.state.pair_stats(pair)
        stats["candles"] += df.height
        stats["bytes"] += size
        stats[source] += 1
        if source == "public":
            self.state.filled_public += 1
        else:
            self.state.filled_rest += 1
        await self.rec.update(**self.state.totals())

    # -------------------------------------------------------- descargas
    @staticmethod
    async def _download_zip(
        client: BinancePublicDataClient, url: str, checksum: str | None
    ) -> Path:
        """Descarga validando checksum; ante mismatch reintenta ``CHECKSUM_RETRIES`` vez (RF-9)."""
        for attempt in range(CHECKSUM_RETRIES):
            try:
                return await client.download(url, checksum)
            except ChecksumMismatchError:
                logger.warning("checksum_mismatch_retry", url=url, attempt=attempt + 1)
        return await client.download(url, checksum)

    async def download_public(
        self,
        item: DownloadPlanItem,
        public_client: BinancePublicDataClient,
        rest_client: BinanceRestClient,
    ) -> None:
        lo, hi = item.covers_range_ms
        async with self.semaphore:
            await self.rec.update(current_symbol=item.symbol, current_timeframe=item.timeframe)
            try:
                zip_path = await self._download_zip(
                    public_client, str(item.url), item.expected_checksum
                )
                size = zip_path.stat().st_size
                try:
                    df = await asyncio.to_thread(
                        read_public_data_zip,
                        zip_path,
                        item.symbol,
                        item.timeframe,
                        item.market_type,
                        lo,
                        hi,
                    )
                finally:
                    zip_path.unlink(missing_ok=True)
            except (
                PublicDataNotFoundError,
                ValueError,
                OSError,
                pl.exceptions.PolarsError,
            ) as exc:
                # RF-9: archivo corrupto (checksum/zip/CSV) o ausente -> el sub-rango pasa a REST.
                logger.warning("public_data_fallback_to_rest", url=item.url, error=str(exc))
                self.state.fallback.extend(
                    plan_rest_fallback(
                        [(lo, hi)],
                        item.symbol,
                        item.timeframe,
                        item.market_type,
                        REST_FALLBACK_LIMIT,
                        rest_client,
                    )
                )
                return
            except httpx.HTTPError as exc:
                await self.hard_error(f"Public Data {item.url}: {exc}")
                return
        await self.collect(item, df, "public", size)

    async def download_rest(self, item: DownloadPlanItem, rest_client: BinanceRestClient) -> None:
        lo, hi = item.covers_range_ms
        limit = int(str((item.params or {}).get("limit", REST_FALLBACK_LIMIT)))
        async with self.semaphore:
            await self.rec.update(current_symbol=item.symbol, current_timeframe=item.timeframe)
            try:
                klines = await rest_client.fetch_klines(
                    item.symbol, item.timeframe, item.market_type, lo, hi, limit
                )
            except httpx.HTTPError as exc:
                await self.hard_error(f"REST {_pair_key(_pair(item))} [{lo}, {hi}]: {exc}")
                return
        df = await asyncio.to_thread(
            normalize_rest_json, klines, item.symbol, item.timeframe, item.market_type, lo, hi
        )
        if df.height == 0:
            await self.unfillable(item)
            return
        await self.collect(item, df, "rest", 0)

    async def download_all(self, transport: httpx.AsyncBaseTransport | None) -> None:
        limiter = build_rate_limiter(self.settings)
        public_items = [i for i in self.plan.items if i.source == "public_data"]
        rest_items = [i for i in self.plan.items if i.source == "rest_api"]
        async with (
            BinancePublicDataClient(limiter, settings=self.settings, transport=transport) as pub,
            BinanceRestClient(limiter, settings=self.settings, transport=transport) as rest,
        ):
            await self.rec.phase("DOWNLOADING_PUBLIC")
            await asyncio.gather(*(self.download_public(i, pub, rest) for i in public_items))
            await self.rec.phase("DOWNLOADING_REST")
            pending = [*rest_items, *self.state.fallback]
            await asyncio.gather(*(self.download_rest(i, rest) for i in pending))

    # ------------------------------------------------ normalizar/escribir
    async def write_all(self) -> list[str]:
        await self.rec.phase("NORMALIZING")
        combined: dict[Pair, pl.DataFrame] = {}
        for pair, frames in self.state.frames.items():
            combined[pair] = await asyncio.to_thread(_combine, frames)

        await self.rec.phase("WRITING_PARQUET")
        parquet_paths: list[str] = []
        for pair in sorted(combined):
            await self.rec.update(current_symbol=pair[0], current_timeframe=pair[1])
            parquet_paths.extend(
                await asyncio.to_thread(
                    write_parquet_partitioned, combined[pair], self.settings.PARQUET_DIR
                )
            )
            self.rec.mark_pairs_done([[pair[0], pair[1]]])
            await self.rec.save_summary()
        return parquet_paths

    async def run(self, transport: httpx.AsyncBaseTransport | None) -> DownloadResult:
        plan_pairs = sorted({_pair(i) for i in self.plan.items})
        self.rec.summary.setdefault("pairs_total", [[s, tf] for s, tf, _ in plan_pairs])
        self.rec.summary.setdefault("gaps_unfillable", [])

        await self.download_all(transport)
        parquet_paths = await self.write_all()
        self.rec.mark_pairs_done([[s, tf] for s, tf, _ in plan_pairs])

        state = self.state
        final: Literal["COMPLETED", "FAILED"] = "FAILED" if state.hard_errors else "COMPLETED"
        await self.rec.update(
            **state.totals(),
            parquet_paths_json=parquet_paths,
            stats_json=state.stats,
            errors_json=self.rec.errors,
            result_summary_json=self.rec.summary,
            error=state.hard_errors[0] if state.hard_errors else None,
        )
        await self.rec.phase(final)
        logger.info(
            "download_finished",
            status=final,
            files=len(parquet_paths),
            unfillable=len(state.unfillable),
            errors=len(self.rec.errors),
        )
        return DownloadResult(
            task_id=self.plan.task_id,
            status=final,
            parquet_paths=parquet_paths,
            stats=dict(state.stats),
            errors=list(self.rec.errors),
            gaps_unfillable=list(state.unfillable),
        )


async def run_download(
    request: DownloadRequest,
    task_id: str,
    *,
    settings: FinancesSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    on_phase: PhaseCallback | None = None,
) -> DownloadResult:
    """Pipeline completo (pasos 1-7) sobre una tarea ya despachada (o la crea)."""
    settings = settings or FinancesSettings()
    await asyncio.to_thread(create_download_task, task_id, request, settings=settings)
    rec = await _TaskRecorder.open_binance(task_id, settings, on_phase)
    with structlog.contextvars.bound_contextvars(task_id=task_id):
        await rec.phase("PLANNING")
        try:
            outcome = await build_download_plan(
                request, task_id=task_id, settings=settings, transport=transport
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            logger.exception("planning_failed", error=str(exc))
            return await rec.fail(f"Planificación fallida: {exc}")

        market = request.market_type.value
        for symbol in outcome.skipped_symbols:
            await rec.add_error(f"Símbolo no existe en exchangeInfo ({market}): {symbol} (omitido)")
        planned = {(i.symbol, i.timeframe) for i in outcome.plan.items}
        rec.mark_pairs_done(
            [p for p in rec.summary.get("pairs_total", []) if (p[0], p[1]) not in planned]
        )
        await rec.update(gaps_found=outcome.gaps_found, result_summary_json=rec.summary)

    return await execute_download_plan(
        outcome.plan,
        request.max_concurrent,
        settings=settings,
        transport=transport,
        _recorder=rec,
    )


def run_download_sync(
    request: DownloadRequest, task_id: str, settings: FinancesSettings | None = None
) -> None:
    """Entrada para ``BackgroundTasks``: corre en el *threadpool* con su propio event loop.

    Nunca propaga excepciones: el fallo queda registrado en la tarea (``FAILED``).
    """
    core_tasks.run_download_sync(lambda: run_download(request, task_id, settings=settings), task_id)


# ----------------------------------------------------------- consultas


def get_progress(
    task_id: str, *, settings: FinancesSettings | None = None
) -> DownloadProgress | None:
    """Progreso granular (mismo DTO para ``GET /status`` y ``ft_market_data_binance status``)."""
    row = load_task_row(task_id, settings)
    if row is None:
        return None
    summary: dict[str, Any] = json_field(row, "result_summary_json", {})
    total = {(p[0], p[1]) for p in summary.get("pairs_total", [])}
    done = {(p[0], p[1]) for p in summary.get("pairs_done", [])} & total
    symbols = {s for s, _ in total}
    timeframes = {tf for _, tf in total}
    status = str(row["status"])
    return DownloadProgress.model_validate(
        {
            "task_id": row["task_id"],
            "status": status,
            "phase": row.get("phase") or status,
            "symbols_total": len(symbols),
            "symbols_done": sum(
                1 for s in symbols if all((s, tf) in done for tf in timeframes if (s, tf) in total)
            ),
            "timeframes_total": len(timeframes),
            "timeframes_done": sum(
                1 for tf in timeframes if all((s, tf) in done for s in symbols if (s, tf) in total)
            ),
            "downloaded_bytes": row.get("downloaded_bytes") or 0,
            "processed_candles": row.get("processed_candles") or 0,
            "gaps_found": row.get("gaps_found") or 0,
            "gaps_filled_public": row.get("gaps_filled_public") or 0,
            "gaps_filled_rest": row.get("gaps_filled_rest") or 0,
            "errors": json_field(row, "errors_json", []),
            "current_symbol": row.get("current_symbol"),
            "current_timeframe": row.get("current_timeframe"),
        }
    )


def get_result(task_id: str, *, settings: FinancesSettings | None = None) -> DownloadResult | None:
    """Resultado final (``GET /result`` y ``ft_market_data_binance result``).

    Raises:
        TaskNotFinishedError: la tarea aún no ha terminado.
    """
    row = load_task_row(task_id, settings)
    if row is None:
        return None
    status = ensure_finished(task_id, row)
    summary: dict[str, Any] = json_field(row, "result_summary_json", {})
    stats: dict[str, object] = json_field(row, "stats_json", {})
    return DownloadResult.model_validate(
        {
            "task_id": row["task_id"],
            "status": status,
            "parquet_paths": json_field(row, "parquet_paths_json", []),
            "stats": stats,
            "errors": json_field(row, "errors_json", []),
            "gaps_unfillable": summary.get("gaps_unfillable", []),
        }
    )
