"""Ciclo de vida de tareas de descarga sobre ``SQLTaskManager`` (Art. 2, Art. 18).

- :func:`create_download_task` registra la tarea en ``PENDING`` al despachar.
- :class:`TaskRecorder` persiste el progreso con escrituras en hilo (Art. 3) y hace
  avanzar las fases de forma monótona según el orden de fases de cada herramienta.
- :func:`run_download_sync` es la entrada para ``BackgroundTasks``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Self

import structlog

from finances_core.config import FinancesSettings
from finances_core.tasks import SQLTaskManager

logger = structlog.get_logger(__name__)

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED"})
MAX_CONCURRENT_ENV = "FT_DOWNLOAD_MAX_CONCURRENT"

PhaseCallback = Callable[[str], None]


class TaskNotFinishedError(Exception):
    """La tarea existe pero aún no está en ``COMPLETED``/``FAILED``."""

    def __init__(self, task_id: str, status: str) -> None:
        super().__init__(f"Task {task_id} not finished: {status}")
        self.status = status


def resolve_max_concurrent(override: int | None, settings: FinancesSettings) -> int:
    """CLI/API > ``FT_DOWNLOAD_MAX_CONCURRENT`` > default (que se crea en el proceso)."""
    if override is not None:
        return override
    value = settings.DOWNLOAD_MAX_CONCURRENT
    if MAX_CONCURRENT_ENV not in os.environ:
        os.environ[MAX_CONCURRENT_ENV] = str(value)
        logger.info(f"Usando default {MAX_CONCURRENT_ENV}={value}")
    return value


def create_download_task(
    task_id: str, summary: dict[str, Any], *, settings: FinancesSettings | None = None
) -> None:
    """Registra la tarea en ``PENDING`` con su resumen inicial (idempotente)."""
    settings = settings or FinancesSettings()
    SQLTaskManager(settings.TASKS_DB_PATH).create_task(
        task_id, status="PENDING", phase="PENDING", result_summary_json=summary
    )


def load_task_row(task_id: str, settings: FinancesSettings | None = None) -> dict[str, Any] | None:
    """Fila de la tarea en ``tasks.db`` o ``None`` si no existe."""
    settings = settings or FinancesSettings()
    return SQLTaskManager(settings.TASKS_DB_PATH).get_task(task_id)


def json_field(row: dict[str, Any], name: str, default: Any) -> Any:
    """Decodifica una columna JSON de la fila (``default`` si está vacía)."""
    raw = row.get(name)
    return json.loads(raw) if raw else default


def ensure_finished(task_id: str, row: dict[str, Any]) -> str:
    """Estado terminal de la fila.

    Raises:
        TaskNotFinishedError: la tarea aún no está en ``COMPLETED``/``FAILED``.
    """
    status = str(row["status"])
    if status not in TERMINAL_STATUSES:
        raise TaskNotFinishedError(task_id, status)
    return status


def _with_terminals(phase_order: Sequence[str]) -> tuple[str, ...]:
    return (*phase_order, *(s for s in ("COMPLETED", "FAILED") if s not in phase_order))


class TaskRecorder:
    """Persistencia del progreso en ``SQLTaskManager`` (escrituras en hilo, Art. 3).

    ``phase_order`` es la secuencia de fases de la herramienta; ``COMPLETED`` y
    ``FAILED`` se añaden al final si no están. Una tarea nunca retrocede de fase.
    """

    def __init__(
        self,
        task_id: str,
        settings: FinancesSettings,
        on_phase: PhaseCallback | None,
        *,
        phase_order: Sequence[str],
    ) -> None:
        self.task_id = task_id
        self.manager = SQLTaskManager(settings.TASKS_DB_PATH)
        self.on_phase = on_phase
        self.phase_order = _with_terminals(phase_order)
        self.summary: dict[str, Any] = {}
        self.errors: list[str] = []
        self._phase_index = 0

    @classmethod
    async def open(
        cls,
        task_id: str,
        settings: FinancesSettings,
        on_phase: PhaseCallback | None,
        *,
        phase_order: Sequence[str],
    ) -> Self:
        """Crea (si no existe) o retoma la tarea, cargando resumen, errores y fase."""
        rec = cls(task_id, settings, on_phase, phase_order=phase_order)
        await asyncio.to_thread(rec.manager.create_task, task_id, "PENDING")
        row = await asyncio.to_thread(rec.manager.get_task, task_id)
        if row is not None:
            rec.summary = json_field(row, "result_summary_json", {})
            rec.errors = list(json_field(row, "errors_json", []))
            status = str(row.get("status") or "PENDING")
            rec._phase_index = rec.phase_order.index(status) if status in rec.phase_order else 0
        return rec

    async def update(self, **fields: Any) -> None:
        await asyncio.to_thread(self.manager.update_task, self.task_id, **fields)

    async def phase(self, status: str) -> None:
        """Avanza de fase sólo hacia delante.

        Raises:
            ValueError: ``status`` no pertenece al orden de fases.
        """
        if status not in self.phase_order:
            msg = f"Fase desconocida {status!r}; orden válido: {self.phase_order}"
            raise ValueError(msg)
        index = self.phase_order.index(status)
        if index <= self._phase_index:
            return
        self._phase_index = index
        await self.update(status=status, phase=status)
        logger.info("task_phase", task_id=self.task_id, phase=status)
        if self.on_phase is not None:
            self.on_phase(status)

    async def add_error(self, message: str) -> None:
        self.errors.append(message)
        await self.update(errors_json=self.errors)

    async def save_summary(self) -> None:
        await self.update(result_summary_json=self.summary)

    def mark_pairs_done(self, pairs: list[list[str]]) -> None:
        done: list[list[str]] = self.summary.setdefault("pairs_done", [])
        for pair in pairs:
            if pair not in done:
                done.append(pair)

    async def mark_failed(self, message: str) -> None:
        """Marca ``FAILED`` con el error detallado; no se avanza a ninguna fase posterior."""
        self.errors.append(message)
        self._phase_index = len(self.phase_order)
        await self.update(
            status="FAILED",
            phase="FAILED",
            error=message,
            errors_json=self.errors,
            result_summary_json=self.summary,
        )
        if self.on_phase is not None:
            self.on_phase("FAILED")


def run_download_sync(factory: Callable[[], Coroutine[Any, Any, object]], task_id: str) -> None:
    """Entrada para ``BackgroundTasks``: corre en el *threadpool* con su propio event loop.

    Nunca propaga excepciones: el fallo queda registrado en la tarea (``FAILED``).
    """
    try:
        asyncio.run(factory())
    except Exception as exc:
        logger.exception("background_download_failed", task_id=task_id, error=str(exc))
