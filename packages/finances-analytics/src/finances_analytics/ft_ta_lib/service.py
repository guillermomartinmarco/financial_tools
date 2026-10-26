"""Capa de servicio de ``ft_ta_lib`` compartida por CLI y API (Art. 1; plan §3.4).

Pipeline RF-1 sobre el núcleo compartido ``finances_core.indicators_store``::

    dispatch_compute: validar + ejecución de prueba (RF-4/RF-5) → existencia (404)
                      → bloqueo (409) → plan → tarea PENDING
    run_compute:      PLANNING (persistir plan) → COMPUTING (por dataset: bloqueo →
                      cargar → validar → motor función a función → write_series
                      → manifiesto) → COMPLETED | FAILED (solo IO/inesperado)

La fuente es de solo lectura; todo se escribe en ``IndicatorStore("ft_ta_lib")`` bajo
``FT_INDICATORS_DIR`` (compartido con ``ft_indicators``, con prefijo ``talib_`` y manifiesto
y bloqueo propios). La exportación (RF-10) solo lee lo persistido.

**Errores por item (RF-11)**: ``output_out_of_range`` o una excepción de TA-Lib en una
función no detienen el dataset ni la tarea. El runner compartido solo transporta errores
por dataset, así que :class:`_ItemErrors` los añade a ``errors_json`` de ``tasks.db`` cada
vez que el runner publica progreso (tras registrar cada dataset y tras cualquier escritura
suya de ``errors_json``); así ``status``/``result`` los muestran antes de ``COMPLETED``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import polars as pl
import structlog
from finances_core.config import FinancesSettings
from finances_core.export import stream_export, validate_output_dir
from finances_core.indicators_store import manifest as manifest_mod
from finances_core.indicators_store import runner, source
from finances_core.indicators_store.manifest import DatasetLockedError
from finances_core.indicators_store.runner import DatasetOutcome, PhaseCallback, ProgressCallback
from finances_core.indicators_store.store import IndicatorStore, SeriesNotFoundError, WriteStats
from finances_core.indicators_store.view import build_wide_view
from finances_core.marketdata import tasks_lifecycle
from finances_core.marketdata.plans import current_ms, new_task_id
from finances_core.marketdata.tasks_lifecycle import TaskNotFinishedError
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    ColumnDtype,
    ComputeItemResult,
    ComputeProgress,
    ComputeResult,
    SeriesExportRequest,
    StoreEntry,
    StoreManifest,
)
from finances_core.schemas.ta_lib import TaLibCatalog, TaLibComputeRequest, TaLibPlan
from finances_core.tasks import SQLTaskManager

from . import catalog as catalog_mod
from . import engine, exporter, planner
from .errors import EmptyExportError, OutputOutOfRangeError, TaLibExecutionError
from .model import ResolvedFunction
from .request import group_functions
from .resolver import NAMESPACE, lookup

logger = structlog.get_logger(__name__)

#: Prefijo del router y de las URLs de *polling* (plan §2.2).
API_PREFIX = "/ta-lib"
#: dtype persistido (RF-7) → tipo de Polars para ``IndicatorStore.write_series``.
_POLARS_DTYPES: dict[ColumnDtype, type[pl.DataType]] = {
    "Float32": pl.Float32,
    "Int16": pl.Int16,
    "Int64": pl.Int64,
}


@dataclass(frozen=True)
class PreparedCompute:
    """Petición validada y planificada, lista para ejecutarse como tarea."""

    task_id: str
    request: TaLibComputeRequest
    plan: TaLibPlan
    items_by_dataset: dict[DatasetIdentifier, list[ResolvedFunction]] = field(default_factory=dict)

    @property
    def items_total(self) -> int:
        return len(self.plan.items)


@dataclass(frozen=True)
class ExportOutcome:
    """Resultado de :func:`export`."""

    rows: int
    path: Path | None  # None si se exportó a un stream
    series: list[str]
    stale: list[str]  # series con velas fuente posteriores a su último cálculo


def _settings(settings: FinancesSettings | None) -> FinancesSettings:
    return settings or FinancesSettings()


def get_store(settings: FinancesSettings | None = None) -> IndicatorStore:
    """Almacén ``ft_ta_lib`` en ``FT_INDICATORS_DIR``.

    Raises:
        ConfigError: SI ``FT_INDICATORS_DIR`` está dentro de ``FT_PARQUET_DIR``.
    """
    return IndicatorStore.from_settings(_settings(settings), NAMESPACE)


def engine_version() -> str:
    """``ta-lib {wrapper} / C {versión C}`` (manifiesto, RF-2/RF-8)."""
    cat = catalog_mod.build_catalog()
    return f"ta-lib {cat.talib_version} / C {cat.ta_lib_c_version}"


# ------------------------------------------------------------------ catálogo


def catalog(group: str | None = None, function: str | None = None) -> TaLibCatalog:
    """Catálogo publicado (``GET /ta-lib/catalog``), filtrado por grupo y/o función.

    Con filtro, ``groups`` solo lista los grupos y funciones devueltos.

    Raises:
        TaLibValidationError: SI el grupo o la función no existen (lista los grupos válidos).
    """
    full = catalog_mod.build_catalog()
    if group is None and function is None:
        return full
    names = {e.function for e in full.functions}
    if group is not None:
        group_functions(group)  # valida el grupo (sin distinción de mayúsculas)
        wanted = group.strip().lower()
        names &= {e.function for e in full.functions if e.group.lower() == wanted}
    if function is not None:
        names &= {lookup(function).name}
    functions = [e for e in full.functions if e.function in names]
    groups = {g: [n for n in ns if n in names] for g, ns in full.groups.items()}
    return full.model_copy(
        update={"functions": functions, "groups": {g: ns for g, ns in groups.items() if ns}}
    )


# ------------------------------------------------------------------ despacho


def check_locks(
    store: IndicatorStore, datasets: Sequence[DatasetIdentifier], settings: FinancesSettings
) -> None:
    """Rechaza datasets bloqueados por otra tarea **viva** de ``ft_ta_lib`` (RF-10).

    El bloqueo de ``ft_indicators`` (``_lock.ft_indicators``) no interfiere; un bloqueo de una
    tarea terminada o inexistente es obsoleto y se ignora.

    Raises:
        DatasetLockedError: con el ``task_id`` dueño del bloqueo.
    """
    alive = manifest_mod.task_liveness(settings.TASKS_DB_PATH)
    for ds in datasets:
        path = manifest_mod.lock_path(store.dataset_dir(ds), NAMESPACE)
        try:
            owner = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        if owner and alive(owner):
            raise DatasetLockedError(ds.path_segment(), NAMESPACE, owner)


def prepare_compute(
    request: TaLibComputeRequest,
    settings: FinancesSettings | None = None,
    *,
    task_id: str | None = None,
) -> PreparedCompute:
    """Valida (incluida la ejecución de prueba) y planifica sin escribir (``--dry-run``).

    Raises:
        TaLibValidationError: función/grupo/parámetro inválido, ``TA_BAD_PARAM`` o columna
            de entrada ausente (400/2).
        SourceNotFoundError: algún dataset no existe (404/1).
        DatasetLockedError: algún dataset está bloqueado por una tarea viva (409/1).
        ConfigError: ``FT_INDICATORS_DIR`` dentro de ``FT_PARQUET_DIR``.
    """
    cfg = _settings(settings)
    store = get_store(cfg)
    plan = planner.plan_items(request, cfg, task_id=task_id or new_task_id())
    by_dataset = planner.items_by_dataset(plan)
    check_locks(store, list(by_dataset), cfg)
    return PreparedCompute(
        task_id=plan.task_id, request=request, plan=plan, items_by_dataset=by_dataset
    )


def create_task(prepared: PreparedCompute, settings: FinancesSettings | None = None) -> None:
    """Registra la tarea ``PENDING`` en ``tasks.db`` (RF-11)."""
    runner.create_compute_task(
        prepared.task_id,
        datasets_total=len(prepared.items_by_dataset),
        items_total=prepared.items_total,
        settings=_settings(settings),
    )


def dispatch_compute(
    request: TaLibComputeRequest, settings: FinancesSettings | None = None
) -> PreparedCompute:
    """Valida, planifica y crea la tarea ``PENDING`` (sin calcular)."""
    prepared = prepare_compute(request, settings)
    create_task(prepared, settings)
    logger.info(
        "compute_dispatched",
        task_id=prepared.task_id,
        datasets=len(prepared.items_by_dataset),
        items=prepared.items_total,
    )
    return prepared


# ------------------------------------------------------------------ cálculo


class _ItemErrors:
    """Errores por item de una tarea (RF-11), fusionados en ``errors_json`` de ``tasks.db``.

    Los hilos de cálculo los registran con :meth:`add`; :meth:`sync` (llamado con cada
    progreso publicado por el runner, que lleva sus propios errores por dataset) persiste
    ``errores del runner + errores por item``. El runner solo reescribe ``errors_json`` al
    añadir un error por dataset o al marcar ``FAILED``, y en ambos casos publica progreso
    justo después, de modo que la fusión siempre se restablece.
    """

    def __init__(self, task_id: str, settings: FinancesSettings) -> None:
        self.task_id = task_id
        self._db_path = settings.TASKS_DB_PATH
        self._errors: list[str] = []
        self._lock = threading.Lock()

    def add(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)

    def sync(self, recorded: Sequence[str]) -> None:
        with self._lock:
            pending = [e for e in self._errors if e not in recorded]
        if pending:
            SQLTaskManager(self._db_path).update_task(
                self.task_id, errors_json=[*recorded, *pending]
            )


def _item_result(
    ds: DatasetIdentifier, output: engine.FunctionResult, stats: WriteStats
) -> ComputeItemResult:
    fn = output.resolved
    return ComputeItemResult(
        dataset=ds,
        series_id=fn.series_id,
        columns=list(fn.columns.values()),
        rows=stats.rows,
        first_ms=stats.first_ms,
        last_ms=stats.last_ms,
        stability=output.stability,
        partitions_written=stats.partitions_written,
        partitions_removed=stats.partitions_removed,
        warnings=list(output.warnings),
    )


@dataclass(frozen=True)
class ComputeContext:
    """Lo que ``compute_dataset`` necesita de la tarea en curso."""

    store: IndicatorStore
    settings: FinancesSettings
    task_id: str
    item_errors: _ItemErrors | None = None


def compute_dataset(
    ds: DatasetIdentifier, items: Sequence[ResolvedFunction], ctx: ComputeContext
) -> DatasetOutcome:
    """Carga la serie completa y calcula y reescribe cada función (RF-1.3–5).

    Una función a la vez (memoria acotada, plan §3.3); cada serie se escribe en cuanto se
    calcula. Se ejecuta en un hilo del runner, con el bloqueo del dataset ya tomado.

    Raises:
        SourceNotFoundError: el dataset desapareció tras el despacho.
        InvalidSourceError: fuente no canónica (Art. 13).
        OSError: error de IO al escribir (la tarea pasa a ``FAILED``).
    """
    label = ds.path_segment()
    store, item_errors = ctx.store, ctx.item_errors
    log = logger.bind(task_id=ctx.task_id, dataset=label, symbol=ds.symbol, timeframe=ds.timeframe)
    started = time.perf_counter()
    required = list(dict.fromkeys(c for fn in items for c in fn.source_columns))
    loaded = source.load_source(ctx.settings, ds, required)
    validated = source.validate_source(loaded.frame, required)
    frame = validated.frame
    rows = frame.height
    version = engine_version()

    results: list[ComputeItemResult] = []
    entries: dict[str, StoreEntry] = {}
    compute_ms = 0.0
    for fn in items:
        fn_log = log.bind(series_id=fn.series_id, function=fn.name, group=fn.meta.group)
        computed = time.perf_counter()
        try:
            output = engine.compute_function(frame, fn)
        except (OutputOutOfRangeError, TaLibExecutionError) as exc:
            message = f"{exc.code}: {label}: {fn.series_id}: {exc}"
            fn_log.warning("function_failed", error=str(exc), code=exc.code)
            if item_errors is not None:
                item_errors.add(message)
            continue
        elapsed_ms = (time.perf_counter() - computed) * 1000
        compute_ms += elapsed_ms
        written = time.perf_counter()
        dtypes = {c: _POLARS_DTYPES[d] for c, d in fn.dtypes.items()}
        stats = store.write_series(ds, fn.series_id, output.frame, dtypes)
        item = _item_result(ds, output, stats)
        results.append(item)
        entries[fn.series_id] = StoreEntry(
            series_id=fn.series_id,
            spec=fn.spec().model_dump(mode="json"),
            columns=item.columns,
            column_dtypes=fn.dtypes,
            engine_version=version,
            rows=stats.rows,
            first_ms=stats.first_ms,
            last_ms=stats.last_ms,
            stability=output.stability,
            computed_at_ms=current_ms(),
        )
        fn_log.info(
            "function_computed",
            rows=stats.rows,
            partitions_written=stats.partitions_written,
            stable_from_ms=output.stability.stable_from_ms,
            compute_duration_ms=round(elapsed_ms, 1),
            write_duration_ms=round((time.perf_counter() - written) * 1000, 1),
            warnings=list(output.warnings),
        )
    elapsed = time.perf_counter() - started
    log.info(
        "dataset_functions_computed",
        functions=len(results),
        failed=len(items) - len(results),
        rows=rows,
        compute_duration_ms=round(compute_ms, 1),
        total_duration_ms=round(elapsed * 1000, 1),
        rows_per_s=round(rows / elapsed, 1) if elapsed > 0 else None,
        null_rows={c: n for c, n in validated.null_counts.items() if n},
    )
    return DatasetOutcome(items=results, entries=entries, rows_computed=rows)


async def run_compute(
    prepared: PreparedCompute,
    settings: FinancesSettings | None = None,
    *,
    on_progress: ProgressCallback | None = None,
    on_phase: PhaseCallback | None = None,
) -> ComputeResult:
    """Ejecuta la tarea despachada (``PLANNING → COMPUTING → COMPLETED | FAILED``)."""
    cfg = _settings(settings)
    store = get_store(cfg)
    task_id = prepared.task_id
    item_errors = _ItemErrors(task_id, cfg)
    ctx = ComputeContext(store=store, settings=cfg, task_id=task_id, item_errors=item_errors)

    def _compute(ds: DatasetIdentifier, items: Sequence[ResolvedFunction]) -> DatasetOutcome:
        return compute_dataset(ds, items, ctx)

    def _progress(progress: ComputeProgress) -> None:
        item_errors.sync(progress.errors)
        if on_progress is not None:
            on_progress(progress)

    structlog.contextvars.bind_contextvars(task_id=task_id)
    try:
        return await runner.run_compute_task(
            task_id,
            prepared.plan,
            prepared.items_by_dataset,
            _compute,
            store,
            cfg,
            plan_prefix=planner.PLAN_PREFIX,
            on_progress=_progress,
            on_phase=on_phase,
        )
    finally:
        structlog.contextvars.unbind_contextvars("task_id")


def run_compute_sync(prepared: PreparedCompute, settings: FinancesSettings | None = None) -> None:
    """Entrada para ``BackgroundTasks``: corre en el *threadpool* con su propio event loop."""
    tasks_lifecycle.run_download_sync(lambda: run_compute(prepared, settings), prepared.task_id)


# ------------------------------------------------------------------ consultas


def get_progress(task_id: str, settings: FinancesSettings | None = None) -> ComputeProgress | None:
    """Progreso de la tarea (``None`` si no existe)."""
    return runner.get_progress(task_id, _settings(settings))


def get_plan(task_id: str, settings: FinancesSettings | None = None) -> TaLibPlan | None:
    """Plan persistido (``None`` si no existe)."""
    return planner.load_plan(task_id, _settings(settings))


def get_result(task_id: str, settings: FinancesSettings | None = None) -> ComputeResult | None:
    """Resultado final (``None`` si la tarea no existe).

    Raises:
        TaskNotFinishedError: SI la tarea aún no terminó.
    """
    return runner.get_result(task_id, _settings(settings))


def get_manifest(
    ds: DatasetIdentifier, settings: FinancesSettings | None = None
) -> StoreManifest | None:
    """Manifiesto ``_manifest.ft_ta_lib.json`` del dataset (``None`` si no existe)."""
    return get_store(settings).read_manifest(ds)


# ---------------------------------------------------------------- exportación


def export(
    ds: DatasetIdentifier,
    req: SeriesExportRequest,
    settings: FinancesSettings | None = None,
    *,
    stream: IO[bytes] | None = None,
) -> ExportOutcome:
    """Exporta la tabla ancha ``talib_*`` de ``ds`` (RF-10) a ``req.output_dir`` o a ``stream``.

    ``req.series`` admite ``series_id`` o specs ``NAME[:k=v,...]`` (se traducen a
    ``series_id``). Sin ``stream``, ``req.output_dir`` es obligatorio. Las columnas
    ``Int16``/``Int64`` conservan su tipo.

    Raises:
        TaLibValidationError: spec de ``series`` inválida o fuera de ``ft_ta_lib`` (400/2).
        ForbiddenOutputDirError: destino dentro de ``FT_PARQUET_DIR``/``FT_INDICATORS_DIR``.
        ExportValidationError: rango invertido o ``stability=drop`` sin estabilidad.
        SeriesNotFoundError: función/columna no calculada o dataset sin cálculos (404/1).
        SourceNotFoundError: ``include_ohlcv`` y la fuente ya no existe (404/1).
        EmptyExportError: el rango no tiene filas (400/3).
        OSError: destino no escribible (500/4).
    """
    cfg = _settings(settings)
    store = get_store(cfg)
    to_stream = stream is not None
    if not to_stream and req.output_dir in {None, "-"}:
        msg = "Debe indicarse output_dir o un stream de salida"
        raise ValueError(msg)
    series = exporter.series_ids(req.series) if req.series is not None else None
    effective = req.model_copy(update={"series": series})

    out_path: Path | None = None
    if not to_stream:
        target = validate_output_dir(str(req.output_dir), [cfg.PARQUET_DIR, cfg.INDICATORS_DIR])
        out_path = target / exporter.export_file_name(ds, req.format)

    try:
        view = build_wide_view(store, ds, effective, settings=cfg)
    except SeriesNotFoundError as exc:
        msg = f"{exc}; ejecute antes `ft_ta_lib compute -d {ds.path_segment()} -f ...`"
        raise SeriesNotFoundError(msg) from exc

    rows = int(view.frame.select(pl.len()).collect().item())
    if rows == 0:
        msg = f"Sin filas en el rango pedido para {ds.path_segment()}"
        raise EmptyExportError(msg)
    if view.stale:
        logger.warning("export_stale", dataset=ds.path_segment(), stale=view.stale)
    destination: Path | IO[bytes] | None = out_path if out_path is not None else stream
    if destination is None:  # inalcanzable: validado al inicio
        msg = "Destino de exportación ausente"
        raise ValueError(msg)
    stream_export(view.frame, req.format, destination)
    logger.info(
        "ta_lib_exported",
        dataset=ds.path_segment(),
        format=req.format,
        rows=rows,
        series=view.series,
        output=str(out_path) if out_path is not None else "stream",
    )
    return ExportOutcome(rows=rows, path=out_path, series=view.series, stale=view.stale)


__all__ = [
    "API_PREFIX",
    "ComputeContext",
    "ExportOutcome",
    "PreparedCompute",
    "TaskNotFinishedError",
    "catalog",
    "check_locks",
    "compute_dataset",
    "create_task",
    "dispatch_compute",
    "engine_version",
    "export",
    "get_manifest",
    "get_plan",
    "get_progress",
    "get_result",
    "get_store",
    "prepare_compute",
    "run_compute",
    "run_compute_sync",
]
