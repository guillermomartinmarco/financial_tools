"""Capa de servicio de ``ft_datasets`` compartida por CLI y API (Art. 1).

- ``materialize`` es una tarea con *polling* (RF-8, ADR-D08) sobre ``TaskRecorder``::

      PENDING -> VALIDATING -> PRECHECKING -> DOWNLOADING -> VERIFYING -> COMPLETED | FAILED

  ``FAILED`` solo si un bloque de descarga falla (exit ≠ 0, *timeout* o ejecutable
  ausente) o ante un error inesperado; un ``MISMATCH`` termina ``COMPLETED`` con
  ``outcome = MISMATCH``. La materialización **nunca** escribe el *lock*.
- ``verify`` y ``lock`` son síncronos (la API los corre en el *threadpool*).
- ``lock`` (RF-7): ``WRITTEN`` / ``UNCHANGED`` (sin reescribir) / ``REJECTED`` (con
  diferencias, o algún ítem ``MISSING``) / ``UPDATED`` (solo con ``update=True``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

import structlog
from finances_core.config import FinancesSettings
from finances_core.datasets import recipes
from finances_core.datasets.errors import DatasetsError, InvalidLockError, InvalidRecipeError
from finances_core.datasets.verify import build_recipe_lock, compute_item_lock, verify_recipe
from finances_core.marketdata import tasks_lifecycle as core_tasks
from finances_core.marketdata.plans import current_ms, new_task_id
from finances_core.marketdata.tasks_lifecycle import (
    PhaseCallback,
    TaskNotFinishedError,
    TaskRecorder,
    ensure_finished,
    json_field,
    load_task_row,
)
from finances_core.schemas.datasets import (
    MATERIALIZE_PHASE_ORDER,
    BlockRun,
    DatasetRecipe,
    ItemLock,
    ItemVerification,
    LockResult,
    MaterializeOutcome,
    MaterializePlan,
    MaterializeProgress,
    MaterializeResult,
    RecipeLock,
    VerifyReport,
    VerifyStatus,
)
from pydantic import BaseModel

from finances_downloader.ft_datasets import materializer
from finances_downloader.ft_datasets.runner import Runner, SubprocessRunner, child_env

logger = structlog.get_logger(__name__)

__all__ = [
    "LockState",
    "RecipeDetail",
    "RecipeItemView",
    "RecipeSummary",
    "TaskNotFinishedError",
    "create_materialize_task",
    "dry_run_plan",
    "get_plan",
    "get_progress",
    "get_result",
    "list_recipes",
    "lock_recipe",
    "new_task_id",
    "prepare",
    "run_materialize",
    "run_materialize_sync",
    "show_recipe",
    "verify",
]

#: Paquetes cuya versión se registra (informativa) en el *lock* (RF-7).
TOOL_VERSION_PACKAGES: tuple[str, ...] = ("finances-downloader", "polars", "yfinance")

LockState = Literal["LOCKED", "UNLOCKED", "RECIPE_CHANGED", "INVALID_LOCK", "INVALID_RECIPE"]


class RecipeSummary(BaseModel):
    """Fila de ``list`` / ``GET /recipes``: receta + estado del *lock* (sin verificar datos)."""

    name: str
    description: str
    items: int
    lock: LockState
    error: str | None = None


class RecipeItemView(BaseModel):
    """Ítem expandido con su clave estable (RF-1)."""

    key: str
    exchange: str
    market_type: str
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int


class RecipeDetail(BaseModel):
    """``show`` / ``GET /recipes/{name}``: receta, ítems expandidos y *lock* si existe."""

    recipe: DatasetRecipe
    items: list[RecipeItemView]
    lock: RecipeLock | None


def _settings(settings: FinancesSettings | None) -> FinancesSettings:
    return settings or FinancesSettings()


def today_utc() -> date:
    """Fecha UTC actual (reloj de RF-2)."""
    return datetime.now(UTC).date()


# ------------------------------------------------------------------ recetas
def list_recipes(settings: FinancesSettings | None = None) -> list[RecipeSummary]:
    """Recetas de ``FT_DATASETS_RECIPES_DIR`` con el estado de su *lock*."""
    cfg = _settings(settings)
    out: list[RecipeSummary] = []
    for name in recipes.list_recipes(cfg.DATASETS_RECIPES_DIR):
        try:
            recipe = recipes.load_recipe(cfg.DATASETS_RECIPES_DIR, name)
        except InvalidRecipeError as exc:
            out.append(
                RecipeSummary(
                    name=name, description="", items=0, lock="INVALID_RECIPE", error=str(exc)
                )
            )
            continue
        state: LockState
        error: str | None = None
        try:
            lock = recipes.load_lock(cfg.DATASETS_RECIPES_DIR, name)
        except InvalidLockError as exc:
            lock, state, error = None, "INVALID_LOCK", str(exc)
        else:
            if lock is None:
                state = "UNLOCKED"
            elif lock.recipe_sha256 != recipe.canonical_sha256():
                state = "RECIPE_CHANGED"
            else:
                state = "LOCKED"
        out.append(
            RecipeSummary(
                name=name,
                description=recipe.description,
                items=len(recipe.items()),
                lock=state,
                error=error,
            )
        )
    return out


def show_recipe(name: str, settings: FinancesSettings | None = None) -> RecipeDetail:
    """Receta expandida y *lock* (RF-1).

    Raises:
        RecipeNotFoundError, InvalidRecipeError, InvalidLockError, OSError.
    """
    cfg = _settings(settings)
    recipe = recipes.load_recipe(cfg.DATASETS_RECIPES_DIR, name)
    lock = recipes.load_lock(cfg.DATASETS_RECIPES_DIR, name)
    items = [
        RecipeItemView.model_validate(item.model_dump() | {"key": item.key})
        for item in recipe.items()
    ]
    return RecipeDetail(recipe=recipe, items=items, lock=lock)


def prepare(
    name: str, settings: FinancesSettings | None = None, today: date | None = None
) -> tuple[DatasetRecipe, RecipeLock | None, Path]:
    """Carga y valida receta (RF-1, RF-2), *lock* y ``FT_DATASETS_DIR`` (RF-11).

    Raises:
        RecipeNotFoundError: receta inexistente.
        InvalidRecipeError: receta inválida o rango no reproducible.
        InvalidLockError: *lock* corrupto o de otro algoritmo.
        ConfigError: ``FT_DATASETS_DIR`` anidado con ``FT_PARQUET_DIR``.
        OSError: error de IO.
    """
    cfg = _settings(settings)
    recipe = recipes.load_recipe(cfg.DATASETS_RECIPES_DIR, name)
    recipes.validate_reproducible(recipe, today or today_utc(), cfg.DATASETS_SETTLE_DAYS)
    lock = recipes.load_lock(cfg.DATASETS_RECIPES_DIR, name)
    return recipe, lock, cfg.require_datasets_dir()


# ------------------------------------------------------------------ verify
def verify(
    name: str, settings: FinancesSettings | None = None, today: date | None = None
) -> VerifyReport:
    """Verificación local de la receta contra su *lock* (RF-5, RF-6).

    Raises:
        DatasetDataError: datos no canónicos en el almacén (exit 5 / HTTP 409).
        Además los de :func:`prepare`.
    """
    recipe, lock, datasets_dir = prepare(name, settings, today)
    return verify_recipe(recipe, lock, datasets_dir)


# ------------------------------------------------------------------ lock
def tool_versions() -> dict[str, str]:
    """Versiones (informativas) de los paquetes que producen los datos (RF-7)."""
    out: dict[str, str] = {}
    for package in TOOL_VERSION_PACKAGES:
        try:
            out[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            out[package] = "unknown"
    return out


def _item_differences(key: str, old: ItemLock | None, new: ItemLock | None) -> list[str]:
    if old is None:
        return [f"{key}: ítem nuevo (no estaba en el lock)"]
    if new is None:
        return [f"{key}: ítem eliminado de la receta"]
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    fields = [f for f in before if before[f] != after.get(f)]
    if not fields:
        return []
    detail = ", ".join(fields)
    if "content_hash" in fields:
        detail += f" ({old.content_hash} -> {new.content_hash})"
    return [f"{key}: {detail}"]


def lock_differences(old: RecipeLock, new: RecipeLock) -> list[str]:
    """Diferencias de contenido entre dos *locks* (ignora ``locked_at`` y ``tool_versions``)."""
    diffs: list[str] = []
    if old.recipe_sha256 != new.recipe_sha256:
        diffs.append(f"recipe_sha256: {old.recipe_sha256} -> {new.recipe_sha256}")
    before = {i.key: i for i in old.items}
    after = {i.key: i for i in new.items}
    for key in [*after, *(k for k in before if k not in after)]:
        diffs += _item_differences(key, before.get(key), after.get(key))
    return diffs


def lock_recipe(
    name: str,
    *,
    update: bool = False,
    settings: FinancesSettings | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> LockResult:
    """Bloqueo explícito (RF-7): calcula los hashes desde el almacén y escribe el *lock*.

    Nunca escribe si algún ítem está ``MISSING``; si el *lock* coincide no lo reescribe;
    si difiere solo lo reescribe con ``update=True`` (log ``warning`` con el diff).

    Raises:
        DatasetDataError: datos no canónicos (exit 5 / HTTP 409). Además los de
            :func:`prepare` y ``OSError`` al escribir.
    """
    cfg = _settings(settings)
    recipe, previous, datasets_dir = prepare(name, cfg, today)
    path = recipes.lock_path(cfg.DATASETS_RECIPES_DIR, name)
    computed = [(item.key, compute_item_lock(datasets_dir, item)) for item in recipe.items()]
    missing = [key for key, lock in computed if lock is None]
    if missing:
        logger.warning("dataset_lock_rejected_missing", recipe=name, missing=missing)
        return LockResult(
            recipe=name,
            outcome="REJECTED",
            lock_path=str(path),
            differences=[
                f"{key}: MISSING (sin filas; nunca se bloquea un dataset vacío)" for key in missing
            ],
        )
    new_lock = build_recipe_lock(
        recipe,
        [lock for _, lock in computed if lock is not None],
        locked_at=now or datetime.now(UTC),
        tool_versions=tool_versions(),
    )
    if previous is None:
        recipes.write_lock(cfg.DATASETS_RECIPES_DIR, new_lock)
        return LockResult(recipe=name, outcome="WRITTEN", lock_path=str(path), differences=[])
    diffs = lock_differences(previous, new_lock)
    if not diffs:
        return LockResult(recipe=name, outcome="UNCHANGED", lock_path=str(path), differences=[])
    if not update:
        logger.warning("dataset_lock_rejected", recipe=name, differences=diffs)
        return LockResult(recipe=name, outcome="REJECTED", lock_path=str(path), differences=diffs)
    recipes.write_lock(cfg.DATASETS_RECIPES_DIR, new_lock)
    logger.warning("dataset_lock_updated", recipe=name, differences=diffs)
    return LockResult(recipe=name, outcome="UPDATED", lock_path=str(path), differences=diffs)


# ------------------------------------------------------------------ materialize
def create_materialize_task(
    task_id: str,
    name: str,
    *,
    verify: bool = True,
    refetch: bool = False,
    settings: FinancesSettings | None = None,
) -> None:
    """Registra la tarea en ``PENDING`` al despachar (idempotente, RF-8)."""
    core_tasks.create_download_task(
        task_id,
        {
            "recipe": name,
            "verify": verify,
            "refetch": refetch,
            "items_total": 0,
            "items_skipped": 0,
            "blocks_total": 0,
            "blocks_done": 0,
            "blocks": [],
        },
        settings=settings,
    )


def _global_status(recipe_changed: bool, items: Sequence[ItemVerification]) -> VerifyStatus:
    if recipe_changed:
        return "RECIPE_CHANGED"
    if any(i.status in {"MISSING", "MISMATCH"} for i in items):
        return "MISMATCH"
    if any(i.status == "UNLOCKED" for i in items):
        return "UNLOCKED"
    return "OK"


def report_from_checks(
    recipe: DatasetRecipe,
    lock: RecipeLock | None,
    checks: Mapping[str, ItemVerification],
    duration_ms: int,
) -> VerifyReport:
    """``VerifyReport`` a partir de verificaciones por ítem (RF-5)."""
    changed = lock is not None and lock.recipe_sha256 != recipe.canonical_sha256()
    items = [checks[item.key] for item in recipe.items()]
    return VerifyReport(
        recipe=recipe.name,
        status=_global_status(changed, items),
        recipe_changed=changed,
        items=items,
        duration_ms=duration_ms,
    )


def dry_run_plan(
    name: str,
    *,
    refetch: bool = False,
    settings: FinancesSettings | None = None,
    today: date | None = None,
) -> MaterializePlan:
    """Plan que ejecutaría ``materialize`` (pre-verificación incluida), sin descargar ni
    borrar nada ni registrar tarea."""
    cfg = _settings(settings)
    recipe, lock, datasets_dir = prepare(name, cfg, today)
    checks = materializer.precheck(recipe, lock, datasets_dir)
    return materializer.build_plan(
        recipe, checks, task_id=new_task_id(), created_at_ms=current_ms(), refetch=refetch
    )


class _Materialization:
    """Una ejecución de la tarea ``materialize`` (pasos de plan §3.7)."""

    def __init__(
        self,
        name: str,
        rec: TaskRecorder,
        settings: FinancesSettings,
        runner: Runner,
        *,
        verify: bool,
        refetch: bool,
        today: date | None,
    ) -> None:
        self.name = name
        self.rec = rec
        self.settings = settings
        self.runner = runner
        self.verify = verify
        self.refetch = refetch
        self.today = today
        self.blocks: list[BlockRun] = []

    def _result(
        self,
        status: Literal["COMPLETED", "FAILED"],
        outcome: MaterializeOutcome,
        report: VerifyReport | None,
    ) -> MaterializeResult:
        return MaterializeResult(
            task_id=self.rec.task_id,
            status=status,
            outcome=outcome,
            recipe=self.name,
            blocks=list(self.blocks),
            verify=report,
            errors=list(self.rec.errors),
        )

    async def fail(self, message: str) -> MaterializeResult:
        """``FAILED`` con el detalle (los datos ya escritos persisten)."""
        self.rec.summary["outcome"] = "DOWNLOAD_FAILED"
        await self.rec.mark_failed(message)
        logger.error("materialize_failed", recipe=self.name, error=message)
        return self._result("FAILED", "DOWNLOAD_FAILED", None)

    async def _download(self, plan: MaterializePlan, datasets_dir: Path) -> str | None:
        """Ejecuta los bloques en secuencia (ADR-D11); devuelve el error del primero que falla."""
        await self.rec.phase("DOWNLOADING")
        env = child_env(self.settings, datasets_dir)
        timeout = float(self.settings.DATASETS_DOWNLOAD_TIMEOUT_S)
        for index, block in enumerate(plan.blocks):
            await self.rec.update(current_symbol=",".join(block.symbols))
            run = await asyncio.to_thread(self.runner.run_block, index, block.argv, env, timeout)
            self.blocks.append(run)
            self.rec.summary["blocks"] = [b.model_dump(mode="json") for b in self.blocks]
            self.rec.summary["blocks_done"] = len(self.blocks)
            await self.rec.save_summary()
            if run.exit_code != 0:
                reason = (
                    f"timeout ({timeout:g} s)"
                    if run.timed_out
                    else f"exit {run.exit_code}"
                    if run.exit_code is not None
                    else "no se pudo lanzar"
                )
                last = run.stderr_tail[-1] if run.stderr_tail else ""
                where = f"{block.argv[0]} {' '.join(block.symbols)}"
                return f"Bloque {index} ({where}): {reason}. {last}".strip()
        return None

    async def run(self) -> MaterializeResult:
        await self.rec.phase("VALIDATING")
        recipe, lock, datasets_dir = await asyncio.to_thread(
            prepare, self.name, self.settings, self.today
        )
        await self.rec.phase("PRECHECKING")
        checks = await asyncio.to_thread(materializer.precheck, recipe, lock, datasets_dir)
        plan = materializer.build_plan(
            recipe,
            checks,
            task_id=self.rec.task_id,
            created_at_ms=current_ms(),
            refetch=self.refetch,
        )
        await asyncio.to_thread(materializer.save_materialize_plan, plan, self.settings)
        summary = self.rec.summary
        summary["items_total"] = len(checks)
        summary["items_skipped"] = len(plan.skipped_items)
        summary["blocks_total"] = len(plan.blocks)
        await self.rec.save_summary()
        logger.info(
            "materialize_planned",
            recipe=self.name,
            skipped=len(plan.skipped_items),
            blocks=len(plan.blocks),
            refetch=len(plan.refetch_items),
        )
        if plan.refetch_items:
            await asyncio.to_thread(materializer.apply_refetch, recipe, plan, datasets_dir)
        if plan.blocks:
            error = await self._download(plan, datasets_dir)
            if error is not None:
                return await self.fail(error)

        report: VerifyReport | None = None
        outcome: MaterializeOutcome = "NOT_VERIFIED"
        if self.verify:
            await self.rec.phase("VERIFYING")
            started = time.perf_counter()
            final = await asyncio.to_thread(materializer.precheck, recipe, lock, datasets_dir)
            report = report_from_checks(
                recipe, lock, final, int((time.perf_counter() - started) * 1000)
            )
            outcome = report.status
            summary["verify_report"] = report.model_dump(mode="json")
        summary["outcome"] = outcome
        await self.rec.save_summary()
        await self.rec.phase("COMPLETED")
        logger.info("materialize_completed", recipe=self.name, outcome=outcome)
        return self._result("COMPLETED", outcome, report)


async def run_materialize(
    name: str,
    task_id: str,
    *,
    settings: FinancesSettings | None = None,
    runner: Runner | None = None,
    verify: bool = True,
    refetch: bool = False,
    on_phase: PhaseCallback | None = None,
    today: date | None = None,
) -> MaterializeResult:
    """Tarea ``materialize`` completa sobre una tarea ya despachada (o la crea).

    Raises:
        Exception: un error inesperado marca ``FAILED`` y se relanza.
    """
    cfg = _settings(settings)
    await asyncio.to_thread(
        create_materialize_task, task_id, name, verify=verify, refetch=refetch, settings=cfg
    )
    rec = await TaskRecorder.open(task_id, cfg, on_phase, phase_order=MATERIALIZE_PHASE_ORDER)
    job = _Materialization(
        name,
        rec,
        cfg,
        runner or SubprocessRunner(),
        verify=verify,
        refetch=refetch,
        today=today,
    )
    with structlog.contextvars.bound_contextvars(task_id=task_id, recipe=name):
        try:
            return await job.run()
        except (DatasetsError, OSError, ValueError) as exc:
            return await job.fail(f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            await job.fail(f"Error inesperado ({type(exc).__name__}): {exc}")
            raise


def run_materialize_sync(
    name: str,
    task_id: str,
    *,
    verify: bool = True,
    refetch: bool = False,
    runner: Runner | None = None,
    settings: FinancesSettings | None = None,
) -> None:
    """Entrada para ``BackgroundTasks``: hilo propio con su event loop; nunca propaga."""
    core_tasks.run_download_sync(
        lambda: run_materialize(
            name, task_id, settings=settings, runner=runner, verify=verify, refetch=refetch
        ),
        task_id,
    )


# ------------------------------------------------------------------ consultas
def get_progress(
    task_id: str, *, settings: FinancesSettings | None = None
) -> MaterializeProgress | None:
    """Progreso (mismo DTO para ``GET /status`` y ``ft_datasets status``)."""
    row = load_task_row(task_id, settings)
    if row is None:
        return None
    summary: dict[str, Any] = json_field(row, "result_summary_json", {})
    return MaterializeProgress.model_validate(
        {
            "task_id": row["task_id"],
            "status": row["status"],
            "recipe": summary.get("recipe", ""),
            "items_total": summary.get("items_total", 0),
            "items_skipped": summary.get("items_skipped", 0),
            "blocks_total": summary.get("blocks_total", 0),
            "blocks_done": summary.get("blocks_done", 0),
            "errors": json_field(row, "errors_json", []),
        }
    )


def get_plan(task_id: str, *, settings: FinancesSettings | None = None) -> MaterializePlan | None:
    """Plan persistido (``GET /plan`` y ``ft_datasets plan``)."""
    return materializer.load_materialize_plan(task_id, settings)


def get_result(
    task_id: str, *, settings: FinancesSettings | None = None
) -> MaterializeResult | None:
    """Resultado final (``GET /result`` y ``ft_datasets result``).

    Raises:
        TaskNotFinishedError: la tarea aún no está en ``COMPLETED``/``FAILED``.
    """
    row = load_task_row(task_id, settings)
    if row is None:
        return None
    status = ensure_finished(task_id, row)
    summary: dict[str, Any] = json_field(row, "result_summary_json", {})
    default_outcome = "DOWNLOAD_FAILED" if status == "FAILED" else "NOT_VERIFIED"
    return MaterializeResult.model_validate(
        {
            "task_id": row["task_id"],
            "status": status,
            "outcome": summary.get("outcome", default_outcome),
            "recipe": summary.get("recipe", ""),
            "blocks": summary.get("blocks", []),
            "verify": summary.get("verify_report"),
            "errors": json_field(row, "errors_json", []),
        }
    )
