"""Identificadores de tarea, validación de rango temporal y persistencia de planes.

Los planes se guardan como JSON en ``<raíz>/share/plans/download_plan_{task_id}.json``
y son genéricos sobre el modelo Pydantic de cada herramienta.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar

import structlog
import uuid6
from pydantic import BaseModel

from finances_core.config import FinancesSettings

logger = structlog.get_logger(__name__)

#: Mensaje exigido para peticiones de datos futuros (binance RF-9, yfinance RF-1).
FUTURE_DATA_MESSAGE = "Cannot download future data"

PlanT = TypeVar("PlanT", bound=BaseModel)


def current_ms() -> int:
    """Instante actual en ms UTC."""
    return int(time.time() * 1000)


def new_task_id() -> str:
    """``task_id`` UUID v7 (Art. 18)."""
    return str(uuid6.uuid7())


def validate_time_range(
    start_ms: int,
    end_ms: int | None,
    timeframes_ms: Mapping[str, int],
    now_ms: int | None = None,
) -> int:
    """Validación local (sin red) del rango pedido. Retorna el ``end_ms`` efectivo.

    - **SI** ``start_ms > now_ms - 1 vela`` (para cualquier timeframe de ``timeframes_ms``)
      **ENTONCES** ``ValueError("Cannot download future data: ...")``.
    - ``end_ms`` ausente o futuro se acota a ``now_ms``.
    - **SI** ``end_ms < start_ms`` **ENTONCES** ``ValueError``.
    """
    now = now_ms if now_ms is not None else current_ms()
    for tf, tf_ms in timeframes_ms.items():
        if start_ms > now - tf_ms:
            msg = (
                f"{FUTURE_DATA_MESSAGE}: start_ms={start_ms} > now_ms - 1 vela "
                f"({tf}) = {now - tf_ms}"
            )
            raise ValueError(msg)

    effective_end = end_ms if end_ms is not None else now
    if effective_end > now:
        logger.warning("end_ms_in_future_clamped", end_ms=effective_end, now_ms=now)
        effective_end = now
    if effective_end < start_ms:
        msg = f"end_ms ({effective_end}) debe ser >= start_ms ({start_ms})"
        raise ValueError(msg)
    return effective_end


def plan_file_path(task_id: str, settings: FinancesSettings | None = None) -> Path:
    """Ruta persistente ``<raíz>/share/plans/download_plan_{task_id}.json``."""
    share_dir = (settings or FinancesSettings()).SHARE_DIR
    return share_dir / "plans" / f"download_plan_{task_id}.json"


def load_plan(
    task_id: str, model: type[PlanT], settings: FinancesSettings | None = None
) -> PlanT | None:
    """Plan persistido de ``task_id`` validado con ``model``, o ``None`` si no existe."""
    path = plan_file_path(task_id, settings)
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def save_plan(task_id: str, plan: BaseModel, settings: FinancesSettings | None = None) -> Path:
    """Persiste ``plan`` como JSON indentado y retorna su ruta."""
    path = plan_file_path(task_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path
