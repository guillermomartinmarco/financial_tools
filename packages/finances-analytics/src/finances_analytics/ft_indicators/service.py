"""Capa de servicio de ``ft_indicators`` compartida por CLI y API (Art. 1; plan §3.8).

Pipeline RF-1 sobre el núcleo compartido ``finances_core.indicators_store``::

    dispatch_compute: validar (RF-5/RF-6) → existencia (404) → bloqueo (409)
                      → plan → tarea PENDING
    run_compute:      PLANNING (persistir plan) → COMPUTING (por dataset: bloqueo →
                      cargar → validar → motor → write_series → manifiesto)
                      → COMPLETED | FAILED (solo IO/inesperado)

La fuente es de solo lectura; todo se escribe en ``IndicatorStore("ft_indicators")``
bajo ``FT_INDICATORS_DIR``. La exportación (RF-9) solo lee lo persistido.
"""

from __future__ import annotations

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
    ComputeItemResult,
    ComputeProgress,
    ComputeResult,
    IndicatorCatalogEntry,
    IndicatorComputeRequest,
    IndicatorPlan,
    SeriesExportRequest,
    StoreEntry,
    StoreManifest,
)

from . import catalog as catalog_mod
from . import engine, exporter, planner
from .catalog import NAMESPACE, ResolvedIndicator
from .errors import EmptyExportError

logger = structlog.get_logger(__name__)

#: Prefijo del router y de las URLs de *polling* (plan §2.2).
API_PREFIX = "/indicators"


@dataclass(frozen=True)
class PreparedCompute:
    """Petición validada y planificada, lista para ejecutarse como tarea."""

    task_id: str
    request: IndicatorComputeRequest
    plan: IndicatorPlan
    items_by_dataset: dict[DatasetIdentifier, list[ResolvedIndicator]] = field(default_factory=dict)

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
    """Almacén ``ft_indicators`` en ``FT_INDICATORS_DIR``.

    Raises:
        ConfigError: SI ``FT_INDICATORS_DIR`` está dentro de ``FT_PARQUET_DIR`` (RF-7).
    """
    return IndicatorStore.from_settings(_settings(settings), NAMESPACE)


def catalog() -> list[IndicatorCatalogEntry]:
    """Catálogo publicado (``GET /indicators/catalog``)."""
    return catalog_mod.catalog()


# ------------------------------------------------------------------ despacho


def check_locks(
    store: IndicatorStore, datasets: Sequence[DatasetIdentifier], settings: FinancesSettings
) -> None:
    """Rechaza datasets bloqueados por otra tarea **viva** de ``ft_indicators`` (RF-8).

    Un bloqueo de una tarea terminada o inexistente es obsoleto y se ignora (el runner lo
    reemplaza al ejecutar).

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
    request: IndicatorComputeRequest,
    settings: FinancesSettings | None = None,
    *,
    task_id: str | None = None,
) -> PreparedCompute:
    """Valida y planifica la petición sin escribir nada (también ``--dry-run``).

    Raises:
        IndicatorValidationError: indicador/parámetro inválido o columna ausente (400/2).
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
    """Registra la tarea ``PENDING`` en ``tasks.db`` (RF-10)."""
    runner.create_compute_task(
        prepared.task_id,
        datasets_total=len(prepared.items_by_dataset),
        items_total=prepared.items_total,
        settings=_settings(settings),
    )


def dispatch_compute(
    request: IndicatorComputeRequest, settings: FinancesSettings | None = None
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


def _item_result(
    ds: DatasetIdentifier, output: engine.IndicatorOutput, stats: WriteStats
) -> ComputeItemResult:
    resolved = output.indicator
    return ComputeItemResult(
        dataset=ds,
        series_id=resolved.indicator_id,
        columns=list(resolved.columns.values()),
        rows=stats.rows,
        first_ms=stats.first_ms,
        last_ms=stats.last_ms,
        stability=output.stability,
        partitions_written=stats.partitions_written,
        partitions_removed=stats.partitions_removed,
        warnings=list(output.warnings),
    )


def compute_dataset(
    ds: DatasetIdentifier,
    items: Sequence[ResolvedIndicator],
    *,
    store: IndicatorStore,
    settings: FinancesSettings,
    task_id: str,
) -> DatasetOutcome:
    """Carga la serie completa, calcula los indicadores y reescribe sus archivos (RF-1.3–5).

    Se ejecuta en un hilo del runner, con el bloqueo del dataset ya tomado.

    Raises:
        SourceNotFoundError: el dataset desapareció tras el despacho.
        InvalidSourceError: fuente no canónica (Art. 13).
        OSError: error de IO al escribir (la tarea pasa a ``FAILED``).
    """
    label = ds.path_segment()
    log = logger.bind(task_id=task_id, dataset=label, symbol=ds.symbol, timeframe=ds.timeframe)
    started = time.perf_counter()
    required = list(dict.fromkeys(c for r in items for c in r.inputs))
    loaded = source.load_source(settings, ds, required)
    validated = source.validate_source(loaded.frame, required)
    result = engine.compute(validated.frame, items)
    compute_ms = (time.perf_counter() - started) * 1000
    rows = validated.frame.height

    results: list[ComputeItemResult] = []
    entries: dict[str, StoreEntry] = {}
    for indicator_id, output in result.outputs.items():
        written = time.perf_counter()
        series = result.series_frame(indicator_id)
        dtypes = dict.fromkeys(series.columns[1:], engine.PERSIST_DTYPE)
        stats = store.write_series(ds, indicator_id, series, dtypes)
        item = _item_result(ds, output, stats)
        results.append(item)
        entries[indicator_id] = StoreEntry(
            series_id=indicator_id,
            spec=output.indicator.spec().model_dump(mode="json"),
            columns=item.columns,
            column_dtypes=dict.fromkeys(item.columns, "Float32"),
            engine_version=f"{NAMESPACE}:{output.indicator.definition.engine_version}",
            rows=stats.rows,
            first_ms=stats.first_ms,
            last_ms=stats.last_ms,
            stability=output.stability,
            computed_at_ms=current_ms(),
        )
        log.info(
            "indicator_computed",
            indicator_id=indicator_id,
            rows=stats.rows,
            partitions_written=stats.partitions_written,
            stable_from_ms=output.stability.stable_from_ms,
            write_duration_ms=round((time.perf_counter() - written) * 1000, 1),
            warnings=list(output.warnings),
        )
    elapsed = time.perf_counter() - started
    log.info(
        "dataset_indicators_computed",
        indicators=len(results),
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

    def _compute(ds: DatasetIdentifier, items: Sequence[ResolvedIndicator]) -> DatasetOutcome:
        return compute_dataset(ds, items, store=store, settings=cfg, task_id=task_id)

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
            on_progress=on_progress,
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


def get_plan(task_id: str, settings: FinancesSettings | None = None) -> IndicatorPlan | None:
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
    """Manifiesto ``_manifest.ft_indicators.json`` del dataset (``None`` si no existe)."""
    return get_store(settings).read_manifest(ds)


# ---------------------------------------------------------------- exportación


def export(
    ds: DatasetIdentifier,
    req: SeriesExportRequest,
    settings: FinancesSettings | None = None,
    *,
    stream: IO[bytes] | None = None,
) -> ExportOutcome:
    """Exporta la tabla ancha de ``ds`` (RF-9) a ``req.output_dir`` o a ``stream``.

    ``req.series`` admite ``indicator_id`` o specs ``name[:k=v,...]`` (se traducen a
    ``indicator_id``). Sin ``stream``, ``req.output_dir`` es obligatorio.

    Raises:
        IndicatorValidationError: spec de ``series`` inválida (400/2).
        ForbiddenOutputDirError: destino dentro de ``FT_PARQUET_DIR``/``FT_INDICATORS_DIR``.
        ExportValidationError: rango invertido o ``stability=drop`` sin estabilidad.
        SeriesNotFoundError: indicador/columna no calculado o dataset sin cálculos (404/1).
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
        msg = f"{exc}; ejecute antes `ft_indicators compute -d {ds.path_segment()} -i ...`"
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
        "indicators_exported",
        dataset=ds.path_segment(),
        format=req.format,
        rows=rows,
        series=view.series,
        output=str(out_path) if out_path is not None else "stream",
    )
    return ExportOutcome(rows=rows, path=out_path, series=view.series, stale=view.stale)


__all__ = [
    "API_PREFIX",
    "ExportOutcome",
    "PreparedCompute",
    "TaskNotFinishedError",
    "catalog",
    "check_locks",
    "compute_dataset",
    "create_task",
    "dispatch_compute",
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
