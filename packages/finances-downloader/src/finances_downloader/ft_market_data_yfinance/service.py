"""Capa de servicio compartida por CLI y API (Art. 1): orquestación y estado de tareas.

Pipeline RF-1/RF-8 sobre el núcleo ``finances_core.marketdata``::

    create_download_task (PENDING) -> PLANNING -> DOWNLOADING -> NORMALIZING
    -> WRITING_PARQUET -> COMPLETED | FAILED

- Los pedidos a Yahoo se ejecutan en hilos (``asyncio.to_thread``) con semáforo
  (``max_concurrent``), Token Bucket y reintentos sólo ante 429/red (RF-7).
- Los pedidos de un mismo símbolo van en secuencia; los símbolos en paralelo.
- ``FAILED`` sólo si algún pedido agota los reintentos; símbolos inexistentes y gaps
  fuera de historial **no** fallan la tarea (RF-8, RF-10). Lo descargado se escribe igual.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

import polars as pl
import structlog
from finances_core.config import FinancesSettings
from finances_core.marketdata import tasks_lifecycle as core_tasks
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.marketdata.plans import current_ms, new_task_id
from finances_core.marketdata.rate_limit import TokenBucket
from finances_core.marketdata.retry import RetryPolicy, build_retrying
from finances_core.marketdata.tasks_lifecycle import (
    PhaseCallback,
    TaskNotFinishedError,
    TaskRecorder,
    ensure_finished,
    json_field,
    load_task_row,
    resolve_max_concurrent,
)
from finances_core.schemas.market_data_yfinance import (
    YF_OHLCV_SCHEMA,
    YF_PHASE_ORDER,
    YFDownloadPlan,
    YFDownloadProgress,
    YFDownloadRequest,
    YFDownloadResult,
    YFPlanItem,
)

from finances_downloader.ft_market_data_yfinance import adapter
from finances_downloader.ft_market_data_yfinance.adapter import (
    FetchFn,
    SymbolNotFoundError,
    YahooBatch,
    YahooTransientError,
    is_retryable,
)
from finances_downloader.ft_market_data_yfinance.events import write_events
from finances_downloader.ft_market_data_yfinance.normalizer import (
    normalize_yahoo_batch,
    resolve_market_type,
)
from finances_downloader.ft_market_data_yfinance.planner import EXCHANGE, build_download_plan
from finances_downloader.ft_market_data_yfinance.timeframes import EVENT_TIMEFRAMES

logger = structlog.get_logger(__name__)

__all__ = [
    "TaskNotFinishedError",
    "build_rate_limiter",
    "create_download_task",
    "get_progress",
    "get_result",
    "new_task_id",
    "run_download",
    "run_download_sync",
    "write_candles",
]

Pair = tuple[str, str]  # (symbol, timeframe)


def _pair_key(pair: Pair) -> str:
    return "/".join(pair)


def build_rate_limiter(settings: FinancesSettings) -> TokenBucket:
    """Token Bucket de Yahoo (RF-7)."""
    return TokenBucket(settings.YFINANCE_RATE_CAPACITY, settings.YFINANCE_RATE_REFILL_RATE)


def write_candles(df: pl.DataFrame, parquet_dir: Path) -> list[str]:
    """Velas canónicas -> Hive ``{parquet_dir}/yfinance/...`` sin pérdida (RF-5)."""
    return write_parquet_partitioned(df, parquet_dir, exchange=EXCHANGE, schema=YF_OHLCV_SCHEMA)


def create_download_task(
    task_id: str, request: YFDownloadRequest, *, settings: FinancesSettings | None = None
) -> None:
    """Registra la tarea en ``PENDING`` (idempotente). Se llama en el despacho (RF-8)."""
    core_tasks.create_download_task(
        task_id,
        {
            "request": request.model_dump(mode="json"),
            "items_total": 0,
            "items_done": 0,
            "events_written": 0,
            "events_paths": [],
            "gaps_unfillable": [],
        },
        settings=settings,
    )


# ---------------------------------------------------------------- estado
class _Recorder(TaskRecorder):
    """``TaskRecorder`` del núcleo con las fases RF-8 y el resultado de yfinance."""

    @classmethod
    async def open_yfinance(
        cls, task_id: str, settings: FinancesSettings, on_phase: PhaseCallback | None
    ) -> _Recorder:
        return await cls.open(task_id, settings, on_phase, phase_order=YF_PHASE_ORDER)

    async def fail(self, message: str) -> YFDownloadResult:
        """Marca ``FAILED`` con el detalle y devuelve el resultado (datos previos intactos)."""
        await self.mark_failed(message)
        return YFDownloadResult(
            task_id=self.task_id,
            status="FAILED",
            parquet_paths=[],
            events_paths=[],
            stats={},
            errors=list(self.errors),
            gaps_unfillable=list(self.summary.get("gaps_unfillable", [])),
        )


class _PlanExecutor:
    """Pasos 6-7 de RF-1 para un plan: descarga, normaliza y escribe."""

    def __init__(
        self,
        plan: YFDownloadPlan,
        max_concurrent: int,
        settings: FinancesSettings,
        rec: _Recorder,
        fetcher: FetchFn,
        now_ms: int,
    ) -> None:
        self.plan = plan
        self.settings = settings
        self.rec = rec
        self.fetcher = fetcher
        self.now_ms = now_ms
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.bucket = build_rate_limiter(settings)
        self.policy = RetryPolicy(
            max_retries=settings.YFINANCE_MAX_RETRIES,
            base_backoff_ms=settings.YFINANCE_BASE_BACKOFF_MS,
        )
        self.batches: list[tuple[YFPlanItem, YahooBatch]] = []
        self.stats: dict[str, dict[str, Any]] = {}
        self.hard_errors: list[str] = []
        self.skipped_symbols: set[str] = set()

    # ---------------------------------------------------------- descarga
    async def _fetch(self, item: YFPlanItem) -> YahooBatch:
        """Un pedido con Token Bucket y reintentos RF-7 (relanza el error si se agotan)."""
        retrying = build_retrying(
            self.policy,
            is_retryable,
            "yahoo_request_retry",
            log_fields={"symbol": item.symbol, "timeframe": item.timeframe},
        )
        batch: YahooBatch | None = None
        async for attempt in retrying:
            with attempt:
                await self.bucket.acquire()
                batch = await asyncio.to_thread(
                    self.fetcher,
                    item.symbol,
                    item.yahoo_interval,
                    item.start_ms,
                    item.end_ms,
                    self.settings.YFINANCE_TIMEOUT_S,
                )
        if batch is None:  # pragma: no cover - tenacity siempre retorna o relanza
            msg = "retry loop ended without result"
            raise RuntimeError(msg)
        return batch

    async def _item_done(self) -> None:
        self.rec.summary["items_done"] = int(self.rec.summary.get("items_done", 0)) + 1
        await self.rec.save_summary()

    async def _download_item(self, item: YFPlanItem) -> None:
        key = _pair_key((item.symbol, item.timeframe))
        window = f"{key} [{item.start_ms}, {item.end_ms})"
        async with self.semaphore:
            await self.rec.update(current_symbol=item.symbol, current_timeframe=item.timeframe)
            try:
                batch = await self._fetch(item)
            except SymbolNotFoundError as exc:
                # RF-10: warning, se omite el símbolo y la tarea sigue.
                logger.warning("symbol_not_found", symbol=item.symbol, error=str(exc))
                self.skipped_symbols.add(item.symbol)
                await self.rec.add_error(f"{exc} (omitido)")
                return
            except YahooTransientError as exc:
                message = f"{window}: reintentos agotados ({exc})"
                logger.exception("download_item_failed", error=message)
                self.skipped_symbols.add(item.symbol)
                self.hard_errors.append(message)
                await self.rec.add_error(message)
                return
        stats = self.stats.setdefault(key, {"candles": 0, "market_type": None, "requests": 0})
        stats["requests"] += 1
        self.batches.append((item, batch))

    async def _download_symbol(self, items: list[YFPlanItem]) -> None:
        for item in items:
            if item.symbol not in self.skipped_symbols:
                await self._download_item(item)
            await self._item_done()

    async def download_all(self) -> None:
        await self.rec.phase("DOWNLOADING")
        by_symbol: dict[str, list[YFPlanItem]] = defaultdict(list)
        for item in self.plan.items:
            by_symbol[item.symbol].append(item)
        await asyncio.gather(*(self._download_symbol(items) for items in by_symbol.values()))

    # ------------------------------------------------ normalizar/escribir
    async def normalize_all(
        self,
    ) -> tuple[dict[Pair, list[pl.DataFrame]], dict[str, list[pl.DataFrame]]]:
        await self.rec.phase("NORMALIZING")
        frames: dict[Pair, list[pl.DataFrame]] = defaultdict(list)
        events: dict[str, list[pl.DataFrame]] = defaultdict(list)
        for item, batch in self.batches:
            df = await asyncio.to_thread(
                normalize_yahoo_batch,
                batch,
                symbol=item.symbol,
                timeframe=item.timeframe,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                now_ms=self.now_ms,
            )
            stats = self.stats[_pair_key((item.symbol, item.timeframe))]
            if df.height:
                frames[(item.symbol, item.timeframe)].append(df)
                stats["candles"] += df.height
                stats["market_type"] = df["market_type"][0]
            elif stats["market_type"] is None and batch.instrument_type is not None:
                stats["market_type"] = resolve_market_type(batch.instrument_type, item.symbol)
            if item.timeframe in EVENT_TIMEFRAMES and batch.events.height:
                events[item.symbol].append(batch.events)
        await self.rec.update(processed_candles=self._candles())
        return frames, events

    def _candles(self) -> int:
        return sum(int(s["candles"]) for s in self.stats.values())

    async def write_all(
        self, frames: dict[Pair, list[pl.DataFrame]], events: dict[str, list[pl.DataFrame]]
    ) -> tuple[list[str], list[str], int]:
        await self.rec.phase("WRITING_PARQUET")
        parquet_dir = self.settings.PARQUET_DIR
        parquet_paths: list[str] = []
        for pair in sorted(frames):
            await self.rec.update(current_symbol=pair[0], current_timeframe=pair[1])
            combined = pl.concat(frames[pair], how="vertical")
            parquet_paths.extend(await asyncio.to_thread(write_candles, combined, parquet_dir))

        events_paths: list[str] = []
        events_written = 0
        for symbol in sorted(events):
            batch = pl.concat(events[symbol], how="vertical").unique(
                subset=["event_time_ms", "event_type"], keep="last"
            )
            path = await asyncio.to_thread(write_events, batch, parquet_dir, symbol)
            if path is not None:
                events_paths.append(path)
                events_written += batch.height
        return parquet_paths, events_paths, events_written

    async def run(self) -> YFDownloadResult:
        await self.download_all()
        frames, events = await self.normalize_all()
        parquet_paths, events_paths, events_written = await self.write_all(frames, events)

        final: Literal["COMPLETED", "FAILED"] = "FAILED" if self.hard_errors else "COMPLETED"
        summary = self.rec.summary
        summary["events_written"] = events_written
        summary["events_paths"] = events_paths
        await self.rec.update(
            processed_candles=self._candles(),
            parquet_paths_json=parquet_paths,
            stats_json=self.stats,
            errors_json=self.rec.errors,
            result_summary_json=summary,
            error=self.hard_errors[0] if self.hard_errors else None,
        )
        await self.rec.phase(final)
        logger.info(
            "download_finished",
            status=final,
            files=len(parquet_paths),
            events_files=len(events_paths),
            errors=len(self.rec.errors),
        )
        return YFDownloadResult(
            task_id=self.plan.task_id,
            status=final,
            parquet_paths=parquet_paths,
            events_paths=events_paths,
            stats=dict(self.stats),
            errors=list(self.rec.errors),
            gaps_unfillable=list(self.plan.gaps_unfillable),
        )


async def run_download(
    request: YFDownloadRequest,
    task_id: str,
    *,
    settings: FinancesSettings | None = None,
    fetcher: FetchFn | None = None,
    on_phase: PhaseCallback | None = None,
    now_ms: int | None = None,
) -> YFDownloadResult:
    """Pipeline completo RF-1 sobre una tarea ya despachada (o la crea).

    Raises:
        Exception: un error inesperado (no de Yahoo) marca ``FAILED`` y se relanza.
    """
    settings = settings or FinancesSettings()
    now = now_ms if now_ms is not None else current_ms()
    await asyncio.to_thread(create_download_task, task_id, request, settings=settings)
    rec = await _Recorder.open_yfinance(task_id, settings, on_phase)
    with structlog.contextvars.bound_contextvars(task_id=task_id):
        await rec.phase("PLANNING")
        try:
            outcome = await asyncio.to_thread(
                build_download_plan, request, task_id=task_id, settings=settings, now_ms=now
            )
        except (ValueError, OSError) as exc:
            logger.exception("planning_failed", error=str(exc))
            return await rec.fail(f"Planificación fallida: {exc}")

        plan = outcome.plan
        rec.summary["items_total"] = len(plan.items)
        rec.summary["items_done"] = 0
        rec.summary["gaps_unfillable"] = [g.model_dump(mode="json") for g in plan.gaps_unfillable]
        for gap in plan.gaps_unfillable:
            logger.warning("gap_unfillable", **gap.model_dump(mode="json"))
        await rec.update(gaps_found=outcome.gaps_found, result_summary_json=rec.summary)

        try:
            concurrency = resolve_max_concurrent(request.max_concurrent, settings)
            executor = _PlanExecutor(
                plan, concurrency, settings, rec, fetcher or adapter.default_fetcher(), now
            )
            return await executor.run()
        except Exception as exc:
            logger.exception("download_execution_failed", error=str(exc))
            await rec.fail(f"Error inesperado ({type(exc).__name__}): {exc}")
            raise


def run_download_sync(
    request: YFDownloadRequest, task_id: str, settings: FinancesSettings | None = None
) -> None:
    """Entrada para ``BackgroundTasks``: hilo propio con su event loop; nunca propaga."""
    core_tasks.run_download_sync(lambda: run_download(request, task_id, settings=settings), task_id)


# ----------------------------------------------------------- consultas
def get_progress(
    task_id: str, *, settings: FinancesSettings | None = None
) -> YFDownloadProgress | None:
    """Progreso (mismo DTO para ``GET /status`` y ``ft_market_data_yfinance status``)."""
    row = load_task_row(task_id, settings)
    if row is None:
        return None
    summary: dict[str, Any] = json_field(row, "result_summary_json", {})
    return YFDownloadProgress.model_validate(
        {
            "task_id": row["task_id"],
            "status": row["status"],
            "items_total": summary.get("items_total", 0),
            "items_done": summary.get("items_done", 0),
            "processed_candles": row.get("processed_candles") or 0,
            "events_written": summary.get("events_written", 0),
            "gaps_found": row.get("gaps_found") or 0,
            "errors": json_field(row, "errors_json", []),
        }
    )


def get_result(
    task_id: str, *, settings: FinancesSettings | None = None
) -> YFDownloadResult | None:
    """Resultado final (``GET /result`` y ``ft_market_data_yfinance result``).

    Raises:
        TaskNotFinishedError: la tarea aún no está en ``COMPLETED``/``FAILED``.
    """
    row = load_task_row(task_id, settings)
    if row is None:
        return None
    status = ensure_finished(task_id, row)
    summary: dict[str, Any] = json_field(row, "result_summary_json", {})
    return YFDownloadResult.model_validate(
        {
            "task_id": row["task_id"],
            "status": status,
            "parquet_paths": json_field(row, "parquet_paths_json", []),
            "events_paths": summary.get("events_paths", []),
            "stats": json_field(row, "stats_json", {}),
            "errors": json_field(row, "errors_json", []),
            "gaps_unfillable": summary.get("gaps_unfillable", []),
        }
    )
