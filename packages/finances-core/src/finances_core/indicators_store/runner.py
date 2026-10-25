"""Orquestación de tareas de cálculo de series (plan de ``ft_indicators`` §3.8, RF-10).

Fases ``PENDING → PLANNING → COMPUTING → COMPLETED | FAILED`` (solo avanzan) sobre
:mod:`finances_core.marketdata.tasks_lifecycle`. Por dataset, con un semáforo de
``FT_INDICATORS_MAX_CONCURRENT`` y en un hilo (Art. 3)::

    lock → compute_dataset (motor de la herramienta: carga, valida, calcula, write_series)
         → manifiesto (solo las entradas calculadas) → unlock

- Errores por dataset (``invalid_source``, ``dataset_not_found``, ``dataset_locked``) se
  acumulan en ``errors`` y la tarea **sigue**.
- ``FAILED`` **solo** ante error de IO (``OSError``) o excepción inesperada; cada archivo
  queda en su versión anterior o en la nueva (escritura atómica) y una re-ejecución lo
  corrige (RF-8).

Genérico sobre el tipo de item del plan (``T``): ``ft_indicators`` y ``ft_ta_lib`` aportan
solo su ``compute_dataset``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import structlog
from pydantic import BaseModel

from finances_core.config import FinancesSettings
from finances_core.indicators_store.manifest import DatasetLockedError
from finances_core.indicators_store.source import InvalidSourceError, SourceNotFoundError
from finances_core.indicators_store.store import IndicatorStore
from finances_core.marketdata import tasks_lifecycle
from finances_core.marketdata.plans import save_plan
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    ComputeItemResult,
    ComputeProgress,
    ComputeResult,
    DispatchResponse,
    StoreEntry,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")

#: Orden de fases RF-10 (``COMPLETED``/``FAILED`` los añade ``TaskRecorder``).
PHASE_ORDER = ("PENDING", "PLANNING", "COMPUTING")

ProgressCallback = Callable[[ComputeProgress], None]
PhaseCallback = Callable[[str], None]


@dataclass
class DatasetOutcome:
    """Resultado de ``compute_dataset`` para un dataset."""

    items: list[ComputeItemResult]
    entries: dict[str, StoreEntry]  # entradas del manifiesto a actualizar
    rows_computed: int
    warnings: list[str] = field(default_factory=list)  # avisos de dataset (null_inputs=N)


ComputeDatasetFn = Callable[[DatasetIdentifier, Sequence[T]], DatasetOutcome]


def _error_code(exc: Exception) -> str:
    if isinstance(exc, DatasetLockedError):
        return "dataset_locked"
    if isinstance(exc, InvalidSourceError | SourceNotFoundError):
        return exc.code
    return "error"


def _initial_summary(datasets_total: int, items_total: int) -> dict[str, Any]:
    return {
        "datasets_total": datasets_total,
        "datasets_done": 0,
        "items_total": items_total,
        "items_done": 0,
        "rows_computed": 0,
        "partitions_written": 0,
        "stability": {},
        "warnings": [],
        "items": [],
        "output_dirs": [],
    }


def create_compute_task(
    task_id: str,
    *,
    datasets_total: int,
    items_total: int,
    settings: FinancesSettings | None = None,
) -> None:
    """Registra la tarea en ``PENDING`` al despachar (idempotente)."""
    tasks_lifecycle.create_download_task(
        task_id, _initial_summary(datasets_total, items_total), settings=settings
    )


def dispatch(task_id: str, base_url: str, prefix: str) -> DispatchResponse:
    """Respuesta de despacho (Art. 18) con URLs absolutas bajo ``{base_url}/{prefix}``."""
    root = f"{base_url.rstrip('/')}/{prefix.strip('/')}"
    return DispatchResponse(
        task_id=task_id,
        status="ACCEPTED",
        poll_url=f"{root}/status/{task_id}",
        plan_url=f"{root}/plan/{task_id}",
        result_url=f"{root}/result/{task_id}",
    )


def _progress_from(
    task_id: str, status: str, summary: Mapping[str, Any], errors: list[str]
) -> ComputeProgress:
    return ComputeProgress.model_validate(
        {
            "task_id": task_id,
            "status": status,
            **{
                k: summary.get(k, 0)
                for k in (
                    "datasets_total",
                    "datasets_done",
                    "items_total",
                    "items_done",
                    "rows_computed",
                    "partitions_written",
                )
            },
            "stability": summary.get("stability", {}),
            "warnings": summary.get("warnings", []),
            "errors": errors,
        }
    )


def get_progress(task_id: str, settings: FinancesSettings | None = None) -> ComputeProgress | None:
    """Progreso persistido de la tarea o ``None`` si no existe."""
    row = tasks_lifecycle.load_task_row(task_id, settings)
    if row is None:
        return None
    return _progress_from(
        task_id,
        str(row["status"]),
        tasks_lifecycle.json_field(row, "result_summary_json", {}),
        list(tasks_lifecycle.json_field(row, "errors_json", [])),
    )


def get_result(task_id: str, settings: FinancesSettings | None = None) -> ComputeResult | None:
    """Resultado final o ``None`` si la tarea no existe.

    Raises:
        TaskNotFinishedError: SI la tarea aún no está en ``COMPLETED``/``FAILED``.
    """
    row = tasks_lifecycle.load_task_row(task_id, settings)
    if row is None:
        return None
    status = tasks_lifecycle.ensure_finished(task_id, row)
    summary = tasks_lifecycle.json_field(row, "result_summary_json", {})
    return ComputeResult.model_validate(
        {
            "task_id": task_id,
            "status": status,
            "items": summary.get("items", []),
            "output_dirs": summary.get("output_dirs", []),
            "warnings": summary.get("warnings", []),
            "errors": list(tasks_lifecycle.json_field(row, "errors_json", [])),
        }
    )


class _Run(Generic[T]):
    """Estado de una ejecución de :func:`run_compute_task`."""

    def __init__(
        self,
        rec: tasks_lifecycle.TaskRecorder,
        store: IndicatorStore,
        compute_dataset: ComputeDatasetFn[T],
        on_progress: ProgressCallback | None,
    ) -> None:
        self.rec = rec
        self.store = store
        self.compute_dataset = compute_dataset
        self.on_progress = on_progress
        self.status = "PENDING"
        self.fatal: list[str] = []
        self._lock = asyncio.Lock()

    def emit(self) -> None:
        if self.on_progress is not None:
            self.on_progress(
                _progress_from(self.rec.task_id, self.status, self.rec.summary, self.rec.errors)
            )

    async def phase(self, status: str) -> None:
        await self.rec.phase(status)
        self.status = status
        self.emit()

    def _process(self, ds: DatasetIdentifier, items: Sequence[T]) -> DatasetOutcome:
        """Hilo de trabajo: bloqueo + motor de la herramienta + manifiesto."""
        with self.store.lock(ds, self.rec.task_id):
            started = time.perf_counter()
            outcome = self.compute_dataset(ds, items)
            if outcome.entries:
                self.store.update_manifest(ds, outcome.entries)
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "dataset_computed",
                task_id=self.rec.task_id,
                dataset=ds.path_segment(),
                symbol=ds.symbol,
                timeframe=ds.timeframe,
                items=len(outcome.items),
                rows=outcome.rows_computed,
                compute_duration_ms=round(elapsed_ms, 1),
            )
            return outcome

    async def dataset(
        self, ds: DatasetIdentifier, items: Sequence[T], semaphore: asyncio.Semaphore
    ) -> None:
        async with semaphore:
            if self.fatal:
                return
            label = ds.path_segment()
            try:
                outcome = await asyncio.to_thread(self._process, ds, items)
            except (InvalidSourceError, SourceNotFoundError, DatasetLockedError) as exc:
                logger.warning(
                    "dataset_skipped", task_id=self.rec.task_id, dataset=label, error=str(exc)
                )
                async with self._lock:
                    self.rec.summary["datasets_done"] += 1
                    await self.rec.add_error(f"{_error_code(exc)}: {label}: {exc}")
                    await self.rec.save_summary()
                    self.emit()
                return
            except Exception as exc:  # IO (OSError) o inesperada → FAILED
                logger.exception("dataset_failed", task_id=self.rec.task_id, dataset=label)
                self.fatal.append(f"{type(exc).__name__}: {label}: {exc}")
                return
            await self._record(ds, outcome)

    async def _record(self, ds: DatasetIdentifier, outcome: DatasetOutcome) -> None:
        label = ds.path_segment()
        summary = self.rec.summary
        async with self._lock:
            summary["datasets_done"] += 1
            summary["items_done"] += len(outcome.items)
            summary["rows_computed"] += outcome.rows_computed
            summary["partitions_written"] += sum(i.partitions_written for i in outcome.items)
            summary["warnings"].extend(f"{label}: {w}" for w in outcome.warnings)
            for item in outcome.items:
                key = f"{label}:{item.series_id}"
                summary["stability"][key] = item.stability.model_dump(mode="json")
                summary["warnings"].extend(f"{key}: {w}" for w in item.warnings)
                summary["items"].append(item.model_dump(mode="json"))
            out_dir = str(self.store.dataset_dir(ds))
            if outcome.items and out_dir not in summary["output_dirs"]:
                summary["output_dirs"].append(out_dir)
            await self.rec.save_summary()
            self.emit()


async def run_compute_task(
    task_id: str,
    plan: BaseModel,
    items_by_dataset: Mapping[DatasetIdentifier, Sequence[T]],
    compute_dataset: ComputeDatasetFn[T],
    store: IndicatorStore,
    settings: FinancesSettings,
    *,
    plan_prefix: str,
    on_progress: ProgressCallback | None = None,
    on_phase: PhaseCallback | None = None,
) -> ComputeResult:
    """Ejecuta la tarea completa y devuelve su :class:`ComputeResult`.

    Nunca propaga errores de cálculo: los por dataset van a ``errors`` y los de IO o
    inesperados dejan la tarea en ``FAILED`` (devuelta en el resultado).
    """
    rec = await tasks_lifecycle.TaskRecorder.open(
        task_id, settings, on_phase, phase_order=PHASE_ORDER
    )
    items_total = sum(len(v) for v in items_by_dataset.values())
    defaults = _initial_summary(len(items_by_dataset), items_total)
    rec.summary = {**defaults, **{k: v for k, v in rec.summary.items() if k in defaults}}
    run = _Run(rec, store, compute_dataset, on_progress)
    run.emit()
    started = time.perf_counter()
    failure: str | None = None
    try:
        await run.phase("PLANNING")
        await asyncio.to_thread(save_plan, task_id, plan, settings, prefix=plan_prefix)
        await rec.save_summary()
        await run.phase("COMPUTING")
        semaphore = asyncio.Semaphore(settings.INDICATORS_MAX_CONCURRENT)
        await asyncio.gather(
            *(run.dataset(ds, items, semaphore) for ds, items in items_by_dataset.items())
        )
    except Exception as exc:  # IO o inesperada fuera de un dataset → FAILED
        logger.exception("compute_task_error", task_id=task_id)
        failure = f"{type(exc).__name__}: {exc}"
    if failure is None and run.fatal:
        failure = "; ".join(run.fatal)
    if failure is None:
        await run.phase("COMPLETED")
    else:
        logger.error("compute_task_failed", task_id=task_id, error=failure)
        await rec.mark_failed(failure)
        run.status = "FAILED"
        run.emit()
    elapsed = time.perf_counter() - started
    rows = int(rec.summary.get("rows_computed", 0))
    logger.info(
        "compute_task_finished",
        task_id=task_id,
        status=run.status,
        rows=rows,
        rows_per_s=round(rows / elapsed, 1) if elapsed > 0 else None,
        compute_duration_ms=round(elapsed * 1000, 1),
    )
    result = get_result(task_id, settings)
    if result is None:  # pragma: no cover - la fila se acaba de escribir
        msg = f"Tarea {task_id} desaparecida de tasks.db"
        raise RuntimeError(msg)
    return result
