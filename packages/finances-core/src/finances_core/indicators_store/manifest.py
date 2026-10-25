"""Manifiesto atómico y bloqueo por dataset y herramienta (plan de ``ft_indicators`` §3.6).

- ``_manifest.{namespace}.json``: validado con :class:`StoreManifest`, escrito con
  temporal + ``os.replace`` **después** de las particiones; solo se actualizan las
  entradas pedidas (las demás se conservan).
- ``_lock.{namespace}``: creado con ``O_EXCL`` y con el ``task_id`` dueño (también en
  ``StoreManifest.lock_task_id``). Un bloqueo es **obsoleto** si su tarea es terminal o
  no existe en ``tasks.db`` (caída) y entonces se reemplaza (RF-8). Los bloqueos de
  ``ft_indicators`` y ``ft_ta_lib`` son archivos distintos: no se excluyen entre sí.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import structlog

from finances_core.indicators_store.namespaces import (
    lock_file_name,
    manifest_file_name,
    validate_series_id,
)
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import Namespace, StoreEntry, StoreManifest
from finances_core.tasks import TERMINAL_STATUSES, SQLTaskManager

logger = structlog.get_logger(__name__)

#: ``task_id -> ¿sigue viva?`` (por defecto, consulta ``tasks.db``).
TaskLiveness = Callable[[str], bool]

_LOCK_ATTEMPTS = 3


class DatasetLockedError(Exception):
    """Otra tarea viva de la misma herramienta está calculando el dataset (RF-8)."""

    def __init__(self, dataset: str, namespace: str, owner_task_id: str) -> None:
        super().__init__(f"Dataset {dataset} bloqueado por la tarea {owner_task_id} ({namespace})")
        self.dataset = dataset
        self.namespace = namespace
        self.owner_task_id = owner_task_id


def manifest_path(dataset_dir: Path, namespace: Namespace) -> Path:
    """``{dataset_dir}/_manifest.{namespace}.json``."""
    return dataset_dir / manifest_file_name(namespace)


def lock_path(dataset_dir: Path, namespace: Namespace) -> Path:
    """``{dataset_dir}/_lock.{namespace}``."""
    return dataset_dir / lock_file_name(namespace)


def read_manifest(dataset_dir: Path, namespace: Namespace) -> StoreManifest | None:
    """Manifiesto del namespace o ``None`` si no existe.

    Raises:
        ValueError: SI el archivo pertenece a otro namespace o no valida.
    """
    path = manifest_path(dataset_dir, namespace)
    if not path.is_file():
        return None
    manifest = StoreManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest.namespace != namespace:
        msg = f"{path} declara namespace {manifest.namespace!r}, se esperaba {namespace!r}"
        raise ValueError(msg)
    return manifest


def write_manifest(dataset_dir: Path, manifest: StoreManifest) -> Path:
    """Escribe ``manifest`` de forma atómica (temporal en el mismo directorio + ``replace``)."""
    for sid in manifest.entries:
        validate_series_id(manifest.namespace, sid)
    path = manifest_path(dataset_dir, manifest.namespace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8", newline="\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def merge_entries(
    dataset_dir: Path,
    namespace: Namespace,
    dataset: DatasetIdentifier,
    entries: Mapping[str, StoreEntry],
) -> StoreManifest:
    """Actualiza solo ``entries`` (las demás se conservan) y persiste el manifiesto."""
    current = read_manifest(dataset_dir, namespace) or StoreManifest(
        namespace=namespace, dataset=dataset
    )
    merged = current.model_copy(update={"entries": {**current.entries, **entries}})
    write_manifest(dataset_dir, merged)
    return merged


def _set_lock_owner(
    dataset_dir: Path, namespace: Namespace, dataset: DatasetIdentifier, owner: str | None
) -> None:
    current = read_manifest(dataset_dir, namespace) or StoreManifest(
        namespace=namespace, dataset=dataset
    )
    write_manifest(dataset_dir, current.model_copy(update={"lock_task_id": owner}))


def task_liveness(tasks_db_path: Path) -> TaskLiveness:
    """Liveness por ``tasks.db``: viva si existe y no está en ``COMPLETED``/``FAILED``."""

    def _alive(task_id: str) -> bool:
        if not task_id:
            return False
        row = SQLTaskManager(tasks_db_path).get_task(task_id)
        return row is not None and str(row["status"]) not in TERMINAL_STATUSES

    return _alive


def _read_owner(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def acquire_lock(
    dataset_dir: Path,
    namespace: Namespace,
    dataset: DatasetIdentifier,
    task_id: str,
    is_alive: TaskLiveness,
) -> None:
    """Toma el bloqueo del dataset para ``task_id`` (``O_EXCL``).

    Raises:
        DatasetLockedError: SI otra tarea viva de la misma herramienta lo tiene.
    """
    path = lock_path(dataset_dir, namespace)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = ""
    for _ in range(_LOCK_ATTEMPTS):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = _read_owner(path)
            if owner == task_id:
                break  # reanudación de la misma tarea
            if is_alive(owner):
                raise DatasetLockedError(dataset.path_segment(), namespace, owner) from None
            logger.warning(
                "stale_lock_replaced",
                dataset=dataset.path_segment(),
                namespace=namespace,
                stale_task_id=owner,
                task_id=task_id,
            )
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(task_id)
        break
    else:
        raise DatasetLockedError(dataset.path_segment(), namespace, owner)
    _set_lock_owner(dataset_dir, namespace, dataset, task_id)


def release_lock(
    dataset_dir: Path, namespace: Namespace, dataset: DatasetIdentifier, task_id: str
) -> None:
    """Libera el bloqueo si sigue perteneciendo a ``task_id``."""
    path = lock_path(dataset_dir, namespace)
    if _read_owner(path) == task_id:
        path.unlink(missing_ok=True)
    manifest = read_manifest(dataset_dir, namespace)
    if manifest is None or manifest.lock_task_id != task_id:
        return
    if manifest.entries:
        _set_lock_owner(dataset_dir, namespace, dataset, None)
    else:  # nada calculado (p. ej. dataset inexistente): no se deja un manifiesto vacío
        manifest_path(dataset_dir, namespace).unlink(missing_ok=True)


@contextmanager
def dataset_lock(
    dataset_dir: Path,
    namespace: Namespace,
    dataset: DatasetIdentifier,
    task_id: str,
    is_alive: TaskLiveness,
) -> Iterator[None]:
    """Contexto ``acquire_lock`` / ``release_lock`` (se libera también ante excepción)."""
    acquire_lock(dataset_dir, namespace, dataset, task_id, is_alive)
    try:
        yield
    finally:
        release_lock(dataset_dir, namespace, dataset, task_id)
