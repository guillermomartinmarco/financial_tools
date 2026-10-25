"""Planificación de ``ft_indicators`` (RF-1.2, RF-5, RF-6; plan §3.5).

:func:`plan_items` es puro (sin cálculo ni escritura): deduplica datasets e indicadores,
resuelve defaults, ``indicator_id`` y columnas por dataset y comprueba que las columnas de
entrada existan (``source=adj_close`` en Binance → validación). El plan se persiste en
``<raíz>/share/plans/indicators_plan_{task_id}.json`` (:func:`save_plan`).
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from finances_core.config import FinancesSettings
from finances_core.indicators_store import source
from finances_core.indicators_store.source import InvalidSourceError
from finances_core.marketdata import plans
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    IndicatorComputeRequest,
    IndicatorPlan,
    IndicatorPlanItem,
)

from .catalog import ResolvedIndicator, resolve
from .request import dedupe_specs

#: Prefijo de los planes persistidos (``indicators_plan_{task_id}.json``).
PLAN_PREFIX = "indicators_plan"


def _columns_of(settings: FinancesSettings, ds: DatasetIdentifier) -> Collection[str] | None:
    """Columnas del dataset (solo el *footer*); ``None`` si es ilegible.

    Un esquema ilegible no invalida la petición: el dataset se omitirá con
    ``invalid_source`` durante la tarea (RF-3, RF-10).

    Raises:
        SourceNotFoundError: SI el dataset no existe.
    """
    try:
        return source.available_columns(settings, ds)
    except InvalidSourceError:
        return None


def plan_items(
    request: IndicatorComputeRequest,
    settings: FinancesSettings,
    *,
    task_id: str,
    created_at_ms: int | None = None,
) -> IndicatorPlan:
    """Plan con un item por (dataset, indicador), ambos deduplicados y en orden de petición.

    Raises:
        IndicatorValidationError: SI una spec es inválida o pide una columna de entrada
            inexistente en el dataset (``adj_close`` en Binance, RF-6).
        SourceNotFoundError: SI algún dataset no existe.
    """
    specs = dedupe_specs(request.indicators)
    items: list[IndicatorPlanItem] = []
    for ds in dict.fromkeys(request.datasets):
        available = _columns_of(settings, ds)
        for spec in specs:
            resolved = resolve(spec, available)
            items.append(
                IndicatorPlanItem(
                    dataset=ds,
                    indicator_id=resolved.indicator_id,
                    spec=resolved.spec(),
                    columns=list(resolved.columns.values()),
                )
            )
    return IndicatorPlan(
        task_id=task_id,
        created_at_ms=created_at_ms if created_at_ms is not None else plans.current_ms(),
        items=items,
    )


def items_by_dataset(plan: IndicatorPlan) -> dict[DatasetIdentifier, list[ResolvedIndicator]]:
    """Indicadores resueltos por dataset (el plan es la única fuente de verdad)."""
    grouped: dict[DatasetIdentifier, list[ResolvedIndicator]] = {}
    for item in plan.items:
        grouped.setdefault(item.dataset, []).append(resolve(item.spec))
    return grouped


def save_plan(plan: IndicatorPlan, settings: FinancesSettings | None = None) -> Path:
    """Persiste ``plan`` en ``share/plans/indicators_plan_{task_id}.json``."""
    return plans.save_plan(plan.task_id, plan, settings, prefix=PLAN_PREFIX)


def load_plan(task_id: str, settings: FinancesSettings | None = None) -> IndicatorPlan | None:
    """Plan persistido de ``task_id`` o ``None`` si no existe."""
    return plans.load_plan(task_id, IndicatorPlan, settings, prefix=PLAN_PREFIX)


__all__ = ["PLAN_PREFIX", "items_by_dataset", "load_plan", "plan_items", "save_plan"]
