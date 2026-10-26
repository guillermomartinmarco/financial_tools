"""Plan de materialización (plan §3.7 pasos 2-3, RF-4).

- **Pre-verificación**: cada ítem se verifica contra el *lock*; los ``OK`` y
  ``OK_WITH_WARNINGS`` se omiten (Art. 12, ADR-D09). Un ítem con datos no canónicos
  (``DatasetDataError``) se trata como ``MISMATCH``.
- **Bloques**: por cada ``[[datasets]]`` de la receta, los símbolos y timeframes de sus
  ítems pendientes (producto cartesiano; un superconjunto es inocuo porque los
  descargadores son idempotentes), rango ``[start_ms, end_ms - 1]``.
- ``--refetch`` borra, solo en ``FT_DATASETS_DIR``, las particiones de los ítems en
  ``MISMATCH`` antes de descargar.
- El plan se persiste en ``<raíz>/share/plans/datasets_plan_{task_id}.json``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import structlog
from finances_core.config import FinancesSettings
from finances_core.datasets import store
from finances_core.datasets.errors import DatasetDataError
from finances_core.datasets.verify import ACTION_MISMATCH, verify_item
from finances_core.marketdata.plans import load_plan, save_plan
from finances_core.schemas.datasets import (
    DatasetRecipe,
    DownloadBlock,
    ItemVerification,
    MaterializePlan,
    RecipeLock,
)

from finances_downloader.ft_datasets.runner import build_argv

logger = structlog.get_logger(__name__)

PLAN_PREFIX = "datasets_plan"
SKIPPABLE = frozenset({"OK", "OK_WITH_WARNINGS"})


def precheck(
    recipe: DatasetRecipe, lock: RecipeLock | None, datasets_dir: Path
) -> dict[str, ItemVerification]:
    """Estado RF-5 de cada ítem contra el *lock* (sin red); datos no canónicos = ``MISMATCH``.

    Raises:
        OSError: error de IO al leer el almacén.
    """
    expected = {i.key: i for i in lock.items} if lock else {}
    out: dict[str, ItemVerification] = {}
    for item in recipe.items():
        locked = expected.get(item.key)
        try:
            out[item.key] = verify_item(datasets_dir, item, locked)
        except DatasetDataError as exc:
            logger.warning("dataset_precheck_invalid_data", item=item.key, error=str(exc))
            out[item.key] = ItemVerification(
                key=item.key,
                status="MISMATCH",
                row_count=0,
                expected_row_count=locked.row_count if locked else None,
                content_hash=None,
                expected_content_hash=locked.content_hash if locked else None,
                warnings=[str(exc)],
                action=ACTION_MISMATCH,
            )
    return out


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def build_blocks(recipe: DatasetRecipe, pending: set[str]) -> list[DownloadBlock]:
    """Un :class:`DownloadBlock` por bloque de la receta con ítems pendientes."""
    blocks: list[DownloadBlock] = []
    for recipe_block in recipe.datasets:
        items = [i for i in recipe_block.items() if i.key in pending]
        if not items:
            continue
        symbols = _unique([i.symbol for i in items])
        timeframes = _unique([i.timeframe for i in items])
        start_ms, end_ms_inclusive = items[0].start_ms, items[0].end_ms - 1
        blocks.append(
            DownloadBlock(
                exchange=recipe_block.exchange,
                market_type=recipe_block.market_type,
                symbols=symbols,
                timeframes=timeframes,
                start_ms=start_ms,
                end_ms_inclusive=end_ms_inclusive,
                argv=build_argv(
                    recipe_block.exchange,
                    recipe_block.market_type,
                    symbols,
                    timeframes,
                    start_ms,
                    end_ms_inclusive,
                ),
            )
        )
    return blocks


def build_plan(
    recipe: DatasetRecipe,
    checks: Mapping[str, ItemVerification],
    *,
    task_id: str,
    created_at_ms: int,
    refetch: bool,
) -> MaterializePlan:
    """Plan a partir de la pre-verificación (ítems omitidos, ``refetch`` y bloques)."""
    keys = [item.key for item in recipe.items()]
    skipped = [k for k in keys if checks[k].status in SKIPPABLE]
    refetch_items = [k for k in keys if refetch and checks[k].status == "MISMATCH"]
    pending = {k for k in keys if k not in skipped}
    return MaterializePlan(
        task_id=task_id,
        created_at_ms=created_at_ms,
        recipe=recipe.name,
        skipped_items=skipped,
        refetch_items=refetch_items,
        blocks=build_blocks(recipe, pending),
    )


def apply_refetch(recipe: DatasetRecipe, plan: MaterializePlan, datasets_dir: Path) -> list[Path]:
    """Borra (solo en ``datasets_dir``) las particiones de ``plan.refetch_items``.

    Raises:
        DatasetDataError: SI algún directorio queda fuera de ``datasets_dir``.
    """
    targets = set(plan.refetch_items)
    deleted: list[Path] = []
    for item in recipe.items():
        if item.key in targets:
            deleted += store.delete_item_partitions(datasets_dir, item)
    return deleted


def save_materialize_plan(plan: MaterializePlan, settings: FinancesSettings) -> Path:
    """Persiste el plan con prefijo ``datasets_plan`` (RF-8)."""
    return save_plan(plan.task_id, plan, settings, prefix=PLAN_PREFIX)


def load_materialize_plan(
    task_id: str, settings: FinancesSettings | None = None
) -> MaterializePlan | None:
    """Plan persistido de ``task_id`` o ``None``."""
    return load_plan(task_id, MaterializePlan, settings, prefix=PLAN_PREFIX)
