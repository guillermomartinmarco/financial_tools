"""Orquestador compartido CLI/API de ft_market_data_manager (Art. 1, ADR M07).

Cada operación: ``plan`` del selector -> por partición leer/transformar -> ``commit``
optimista -> evento de auditoría RF-8. Los errores de una partición se acumulan en su
``PartitionResult`` y nunca abortan las restantes (RF-6). La configuración se lee de
``FinancesSettings()`` en cada llamada, nunca de un singleton.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import polars as pl
import structlog
from finances_core.config import FinancesSettings
from finances_core.marketdata.hive_writer import DEFAULT_FILE_NAME
from finances_core.marketdata.months import month_bounds
from finances_core.schemas.market_data_manager import (
    ManagerRequest,
    ManagerResult,
    PartitionResult,
    PruneRequest,
)

from finances_downloader.ft_market_data_manager import planner
from finances_downloader.ft_market_data_manager.errors import (
    ConfirmationRequiredError,
    InvalidArgumentError,
    PartitionReadError,
)

logger = structlog.get_logger(__name__, component="ft_market_data_manager")

TS_COL = "open_time_ms"

Action = Literal["compact", "dedupe", "prune"]
#: ``(datos leídos, nº de archivos) -> (nuevo contenido | None si no cambia, detalle)``.
Transform = Callable[[pl.DataFrame, int], tuple[pl.DataFrame | None, str | None]]
#: Gancho de progreso opcional: ``(particiones procesadas, total, resultado de la última)``.
#: Lo usan la CLI (barra Rich) y la API (contadores de la tarea); no altera el resultado.
PartitionCallback = Callable[[int, int, PartitionResult], None]


# --- Lectura y forma canónica -----------------------------------------------------------


def _read_partition(partition: planner.Partition) -> pl.DataFrame:
    """Concatena los archivos en orden lexicográfico y valida la forma canónica.

    Raises:
        PartitionReadError: SI un archivo no se puede leer, los esquemas difieren entre
            archivos o ``open_time_ms`` falta, no es ``Int64`` o contiene nulos.
    """
    try:
        frames = [pl.read_parquet(f) for f in partition.files]
        df = pl.concat(frames, how="vertical")
    except (OSError, pl.exceptions.PolarsError, ValueError) as exc:
        msg = f"partición ilegible o con esquemas incompatibles: {exc}"
        raise PartitionReadError(msg) from exc
    if TS_COL not in df.columns:
        msg = f"falta la columna {TS_COL}"
        raise PartitionReadError(msg)
    if df.schema[TS_COL] != pl.Int64:
        msg = f"{TS_COL} debe ser Int64, es {df.schema[TS_COL]}"
        raise PartitionReadError(msg)
    if df[TS_COL].null_count():
        msg = f"{TS_COL} contiene nulos"
        raise PartitionReadError(msg)
    return df


def _encoded_size(df: pl.DataFrame) -> int:
    """Tamaño que tendría ``data.parquet`` (mismos parámetros que ``atomic_write``), sin disco."""
    buffer = io.BytesIO()
    df.write_parquet(buffer, compression="zstd", compression_level=3, statistics=True)
    return buffer.tell()


# --- Transformaciones --------------------------------------------------------------------


def _compact_transform(df: pl.DataFrame, n_files: int) -> tuple[pl.DataFrame | None, str | None]:
    out = df.sort(TS_COL, maintain_order=True)
    if n_files == 1 and out.equals(df):
        return None, None
    return out, None


def _dedupe_transform(df: pl.DataFrame, n_files: int) -> tuple[pl.DataFrame | None, str | None]:
    del n_files
    out = df.unique(subset=TS_COL, keep="last", maintain_order=True).sort(TS_COL)
    removed = df.height - out.height
    if removed == 0:
        return None, None
    example = min(df.filter(pl.col(TS_COL).is_duplicated())[TS_COL].to_list())
    return out, f"duplicados eliminados: {removed}; ejemplo open_time_ms: {example}"


# --- Auditoría (RF-8) --------------------------------------------------------------------


def _audit(action: Action, result: PartitionResult, *, dry_run: bool) -> None:
    logger.info(
        "market_data_manager_audit",
        action=action,
        dataset=result.dataset,
        partition=f"{result.year:04d}-{result.month:02d}",
        rows_before=result.rows_before,
        rows_after=result.rows_after,
        bytes_before=result.bytes_before,
        bytes_after=result.bytes_after,
        dry_run=dry_run,
        result=result.result,
    )


def _finish(
    action: Action, request: ManagerRequest, results: list[PartitionResult], started: float
) -> ManagerResult:
    logger.info(
        "market_data_manager_run",
        action=action,
        dry_run=request.dry_run,
        duration_ms=int((time.monotonic() - started) * 1000),
        partitions_scanned=len(results),
        rows_processed=sum(r.rows_before for r in results),
    )
    return ManagerResult(action=action, dry_run=request.dry_run, partitions=results)


def _result(
    partition: planner.Partition,
    result: Literal["changed", "unchanged", "conflict", "error", "planned"],
    *,
    rows_before: int = 0,
    rows_after: int = 0,
    detail: str | None = None,
) -> PartitionResult:
    """Resultado "sin cambios en disco": archivos y bytes de después = los de antes."""
    return PartitionResult(
        dataset=partition.label,
        year=partition.year,
        month=partition.month,
        result=result,
        files_before=len(partition.files),
        files_after=len(partition.files),
        rows_before=rows_before,
        rows_after=rows_after,
        bytes_before=partition.size_bytes,
        bytes_after=partition.size_bytes,
        detail=detail,
    )


def _after(result: PartitionResult, files: int, size: int) -> PartitionResult:
    return result.model_copy(update={"files_after": files, "bytes_after": size})


# --- compact / dedupe --------------------------------------------------------------------


def _leftover_result(
    partition: planner.Partition,
    rows_before: int,
    rows_after: int,
    exc: planner.LeftoverCleanupError,
) -> PartitionResult:
    """``data.parquet`` se reemplazó pero un sobrante no se pudo borrar: estado real en disco."""
    names = ", ".join(sorted(f.name for f in exc.leftovers))
    partial = _result(
        partition,
        "error",
        rows_before=rows_before,
        rows_after=rows_after,
        detail=(
            f"IO: {exc}. data.parquet se reemplazó pero quedan archivos sin borrar ({names}) "
            "que duplican filas; ejecute dedupe para limpiarlos"
        ),
    )
    size = (partition.directory / DEFAULT_FILE_NAME).stat().st_size
    return _after(partial, 1 + len(exc.leftovers), size)


def _commit_rewrite(
    partition: planner.Partition,
    out: pl.DataFrame,
    rows: int,
    detail: str | None,
    parquet_dir: Path,
) -> PartitionResult:
    """Escribe ``out`` en la partición y traduce el desenlace a ``PartitionResult``."""
    try:
        outcome = planner.commit(partition, out, parquet_dir)
    except planner.LeftoverCleanupError as exc:
        return _leftover_result(partition, rows, out.height, exc)
    except OSError as exc:
        return _result(partition, "error", rows_before=rows, rows_after=rows, detail=f"IO: {exc}")
    if outcome == "conflict":
        return _result(
            partition,
            "conflict",
            rows_before=rows,
            rows_after=rows,
            detail="la partición cambió durante la operación; no se modificó",
        )
    changed = _result(partition, "changed", rows_before=rows, rows_after=out.height, detail=detail)
    return _after(changed, 1, (partition.directory / DEFAULT_FILE_NAME).stat().st_size)


def _rewrite_partition(
    partition: planner.Partition, parquet_dir: Path, transform: Transform, *, dry_run: bool
) -> PartitionResult:
    try:
        df = _read_partition(partition)
    except PartitionReadError as exc:
        return _result(partition, "error", detail=str(exc))
    rows = df.height
    out, detail = transform(df, len(partition.files))
    if out is None:
        return _result(partition, "unchanged", rows_before=rows, rows_after=rows)
    if dry_run:
        planned = _result(
            partition, "planned", rows_before=rows, rows_after=out.height, detail=detail
        )
        return _after(planned, 1, _encoded_size(out))
    return _commit_rewrite(partition, out, rows, detail, parquet_dir)


def _run_rewrite(
    action: Action,
    request: ManagerRequest,
    transform: Transform,
    on_partition: PartitionCallback | None,
) -> ManagerResult:
    started = time.monotonic()
    parquet_dir = FinancesSettings().PARQUET_DIR
    partitions = planner.plan(request, parquet_dir)
    results: list[PartitionResult] = []
    for partition in partitions:
        result = _rewrite_partition(partition, parquet_dir, transform, dry_run=request.dry_run)
        _audit(action, result, dry_run=request.dry_run)
        results.append(result)
        if on_partition is not None:
            on_partition(len(results), len(partitions), result)
    return _finish(action, request, results, started)


def run_compact(
    request: ManagerRequest, on_partition: PartitionCallback | None = None
) -> ManagerResult:
    """RF-1: un único ``data.parquet`` ordenado por partición, sin descartar filas."""
    return _run_rewrite("compact", request, _compact_transform, on_partition)


def run_dedupe(
    request: ManagerRequest, on_partition: PartitionCallback | None = None
) -> ManagerResult:
    """RF-2: elimina ``open_time_ms`` repetidos conservando la última fila en orden de lectura."""
    return _run_rewrite("dedupe", request, _dedupe_transform, on_partition)


# --- prune -------------------------------------------------------------------------------


def _row_count(partition: planner.Partition) -> tuple[int, str | None]:
    try:
        total = sum(
            int(pl.scan_parquet(f).select(pl.len()).collect().item()) for f in partition.files
        )
    except (OSError, pl.exceptions.PolarsError, ValueError):
        return 0, "no se pudo contar las filas (archivo ilegible)"
    return total, None


def _prune_partition(
    partition: planner.Partition, parquet_dir: Path, *, dry_run: bool
) -> PartitionResult:
    rows, note = _row_count(partition)
    if dry_run:
        return _after(_result(partition, "planned", rows_before=rows, detail=note), 0, 0)
    try:
        outcome = planner.commit(partition, None, parquet_dir)
    except OSError as exc:
        return _result(partition, "error", rows_before=rows, rows_after=rows, detail=f"IO: {exc}")
    if outcome == "conflict":
        return _result(
            partition,
            "conflict",
            rows_before=rows,
            rows_after=rows,
            detail="la partición cambió durante la operación; no se borró",
        )
    return _after(_result(partition, "changed", rows_before=rows, detail=note), 0, 0)


def _prune_candidates(request: PruneRequest, parquet_dir: Path) -> list[planner.Partition]:
    """Particiones del selector; con ``before_ms`` solo las que terminan en o antes de T."""
    partitions = planner.plan(request, parquet_dir)
    cutoff = request.before_ms
    if cutoff is None:
        return partitions
    # La que contiene T o es posterior no se toca: el borrado nunca recorta filas.
    return [p for p in partitions if month_bounds(p.year, p.month)[1] <= cutoff]


def run_prune(
    request: PruneRequest, on_partition: PartitionCallback | None = None
) -> ManagerResult:
    """RF-3: borra particiones mensuales completas (``before_ms`` o ``all``).

    Raises:
        ConfirmationRequiredError: SI no es ``dry_run`` y falta ``confirm``; no se borra nada.
    """
    if not (request.dry_run or request.confirm):
        msg = "prune exige confirm=true (o --force en la CLI) salvo con dry_run"
        raise ConfirmationRequiredError(msg)
    started = time.monotonic()
    parquet_dir = FinancesSettings().PARQUET_DIR
    candidates = _prune_candidates(request, parquet_dir)
    results: list[PartitionResult] = []
    for partition in candidates:
        result = _prune_partition(partition, parquet_dir, dry_run=request.dry_run)
        _audit("prune", result, dry_run=request.dry_run)
        results.append(result)
        if on_partition is not None:
            on_partition(len(results), len(candidates), result)
    return _finish("prune", request, results, started)


def validate_selector(request: ManagerRequest) -> None:
    """Valida el rango temporal del selector (compartido por la CLI y la API).

    Raises:
        InvalidArgumentError: SI ``start_ms`` es posterior a ``end_ms``.
    """
    start, end = request.start_ms, request.end_ms
    if start is not None and end is not None and start > end:
        msg = "start_ms no puede ser posterior a end_ms"
        raise InvalidArgumentError(msg)
