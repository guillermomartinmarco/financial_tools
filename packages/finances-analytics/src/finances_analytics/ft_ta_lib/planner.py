"""Planificación de ``ft_ta_lib`` (RF-1.2, RF-3, RF-5; plan §3.4).

:func:`plan_items` no calcula ni escribe: expande grupos (solo funciones ``supported``),
resuelve defaults, ``series_id``, columnas y dtypes, deduplica por ``series_id`` y comprueba
por dataset que las columnas de entrada existan (``price=adj_close`` en Binance →
validación). El plan se persiste en ``<raíz>/share/plans/ta_lib_plan_{task_id}.json``
(:func:`save_plan`).
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from finances_core.config import FinancesSettings
from finances_core.indicators_store import source
from finances_core.indicators_store.source import InvalidSourceError
from finances_core.marketdata import plans
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.ta_lib import TaLibComputeRequest, TaLibPlan, TaLibPlanItem

from .model import ResolvedFunction
from .request import expand
from .resolver import resolve

#: Prefijo de los planes persistidos (``ta_lib_plan_{task_id}.json``).
PLAN_PREFIX = "ta_lib_plan"

__all__ = ["PLAN_PREFIX", "items_by_dataset", "load_plan", "plan_items", "save_plan"]


def _columns_of(settings: FinancesSettings, ds: DatasetIdentifier) -> Collection[str] | None:
    """Columnas del dataset (solo el *footer*); ``None`` si es ilegible.

    Un esquema ilegible no invalida la petición: el dataset se omitirá con
    ``invalid_source`` durante la tarea (RF-11).

    Raises:
        SourceNotFoundError: SI el dataset no existe.
    """
    try:
        return source.available_columns(settings, ds)
    except InvalidSourceError:
        return None


def plan_items(
    request: TaLibComputeRequest,
    settings: FinancesSettings,
    *,
    task_id: str,
    created_at_ms: int | None = None,
) -> TaLibPlan:
    """Plan con un item por (dataset, función), ambos deduplicados y en orden de petición.

    Raises:
        TaLibValidationError: SI una función/grupo/parámetro es inválido, TA-Lib rechaza la
            combinación (``TA_BAD_PARAM``), hay más de 200 funciones tras expandir o una
            entrada usa una columna ausente en el dataset (``adj_close`` en Binance).
        SourceNotFoundError: SI algún dataset no existe.
    """
    functions = expand(request)
    items: list[TaLibPlanItem] = []
    for ds in dict.fromkeys(request.datasets):
        available = _columns_of(settings, ds)
        for fn in functions:
            resolved = resolve(fn.spec(), available) if available is not None else fn
            items.append(
                TaLibPlanItem(
                    dataset=ds,
                    series_id=resolved.series_id,
                    spec=resolved.spec(),
                    columns=resolved.dtypes,
                    lookback=resolved.lookback,
                )
            )
    return TaLibPlan(
        task_id=task_id,
        created_at_ms=created_at_ms if created_at_ms is not None else plans.current_ms(),
        items=items,
    )


def items_by_dataset(plan: TaLibPlan) -> dict[DatasetIdentifier, list[ResolvedFunction]]:
    """Funciones resueltas por dataset (el plan es la única fuente de verdad)."""
    grouped: dict[DatasetIdentifier, list[ResolvedFunction]] = {}
    for item in plan.items:
        grouped.setdefault(item.dataset, []).append(resolve(item.spec))
    return grouped


def save_plan(plan: TaLibPlan, settings: FinancesSettings | None = None) -> Path:
    """Persiste ``plan`` en ``share/plans/ta_lib_plan_{task_id}.json``."""
    return plans.save_plan(plan.task_id, plan, settings, prefix=PLAN_PREFIX)


def load_plan(task_id: str, settings: FinancesSettings | None = None) -> TaLibPlan | None:
    """Plan persistido de ``task_id`` o ``None`` si no existe."""
    return plans.load_plan(task_id, TaLibPlan, settings, prefix=PLAN_PREFIX)
