"""Recorrido del almacén y escritura/borrado con verificación optimista (plan 3.1, RF-6).

``plan`` enumera las particiones mensuales seleccionadas y fotografía sus archivos
(nombre, tamaño, ``mtime_ns``). ``commit`` repite esa fotografía inmediatamente antes de
reemplazar o borrar: si cambió (p. ej. un descargador escribió en paralelo) devuelve
``"conflict"`` sin tocar nada. La escritura va siempre por ``atomic_write`` y los archivos
sobrantes se eliminan solo después del reemplazo.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import polars as pl
from finances_core.marketdata.dataset_layout import (
    dataset_files,
    dataset_label,
    discover_datasets,
    partition_key,
)
from finances_core.marketdata.hive_writer import DEFAULT_FILE_NAME, atomic_write
from finances_core.marketdata.months import month_bounds
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.market_data_manager import DatasetSelector

#: ``(nombre, tamaño, mtime_ns)`` por archivo, ordenado por nombre.
Snapshot = tuple[tuple[str, int, int], ...]
CommitResult = Literal["changed", "conflict"]

_MONTHS = range(1, 13)


@dataclass(frozen=True)
class Partition:
    """Una partición mensual de un dataset con la fotografía de sus archivos."""

    dataset: DatasetIdentifier
    year: int
    month: int
    directory: Path
    files: tuple[Path, ...]
    snapshot: Snapshot

    @property
    def label(self) -> str:
        return dataset_label(self.dataset)

    @property
    def size_bytes(self) -> int:
        return sum(size for _, size, _ in self.snapshot)

    @property
    def period(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def parquet_files(directory: Path) -> list[Path]:
    """Archivos Parquet de una partición, en orden lexicográfico (orden de lectura)."""
    return sorted(directory.glob("*.parquet"))


def take_snapshot(files: list[Path]) -> Snapshot:
    entries: list[tuple[str, int, int]] = []
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue  # desaparecido entre el listado y el stat: la comparación lo detecta
        entries.append((path.name, st.st_size, st.st_mtime_ns))
    return tuple(entries)


def _matches(value: str, wanted: list[str]) -> bool:
    return not wanted or value in wanted


def _in_range(year: int, month: int, selector: DatasetSelector) -> bool:
    start, next_start = month_bounds(year, month)
    if selector.start_ms is not None and next_start <= selector.start_ms:
        return False
    return selector.end_ms is None or start <= selector.end_ms


def plan(selector: DatasetSelector, parquet_dir: Path) -> list[Partition]:
    """Particiones (dataset x mes) que cumplen el selector, con su fotografía de archivos."""
    partitions: list[Partition] = []
    for ident, root in discover_datasets(parquet_dir):
        if not (
            _matches(ident.exchange, selector.exchange)
            and _matches(ident.market_type, selector.market_type)
            and _matches(ident.symbol, selector.symbol)
            and _matches(ident.timeframe, selector.timeframe)
        ):
            continue
        by_month: dict[tuple[int, int], list[Path]] = defaultdict(list)
        for file in dataset_files(root):
            by_month[partition_key(file)].append(file)
        for (year, month), files in sorted(by_month.items()):
            if month not in _MONTHS or year < 1 or not _in_range(year, month, selector):
                continue
            ordered = sorted(files)
            partitions.append(
                Partition(
                    dataset=ident,
                    year=year,
                    month=month,
                    directory=ordered[0].parent,
                    files=tuple(ordered),
                    snapshot=take_snapshot(ordered),
                )
            )
    return partitions


def _remove_empty_hive_dirs(directory: Path, parquet_dir: Path) -> None:
    """Sube desde ``directory`` eliminando directorios vacíos hasta ``parquet_dir`` (exclusive)."""
    stop = parquet_dir.resolve()
    current = directory
    while current.resolve() != stop and stop in current.resolve().parents:
        try:
            current.rmdir()  # falla si no está vacío: se conserva
        except OSError:
            return
        current = current.parent


class LeftoverCleanupError(OSError):
    """``data.parquet`` ya se reemplazó pero no se pudieron borrar archivos sobrantes.

    No se pierden filas, pero hasta que se vuelva a ejecutar el sobrante duplica filas del
    nuevo ``data.parquet``; ``dedupe`` las elimina (RF-6: se informa el estado real).
    """

    def __init__(self, leftovers: list[Path], cause: OSError) -> None:
        self.leftovers = leftovers
        super().__init__(f"{cause}")


def commit(partition: Partition, df: pl.DataFrame | None, parquet_dir: Path) -> CommitResult:
    """Reemplaza la partición por ``df`` (``data.parquet``) o la borra si ``df`` es ``None``.

    Re-verifica la fotografía inmediatamente antes; si cambió devuelve ``"conflict"`` sin
    tocar nada. Los archivos sobrantes se eliminan solo tras el reemplazo atómico, y tras un
    borrado se eliminan los directorios Hive que queden vacíos.

    Raises:
        OSError: SI falla la escritura o el borrado (IO); la partición queda en su versión
            anterior si la falla ocurre antes del reemplazo.
    """
    if take_snapshot(parquet_files(partition.directory)) != partition.snapshot:
        return "conflict"
    if df is None:
        for file in partition.files:
            file.unlink(missing_ok=True)
        _remove_empty_hive_dirs(partition.directory, parquet_dir)
        return "changed"
    target = partition.directory / DEFAULT_FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)  # particiones nuevas (import --create)
    atomic_write(df, target)
    leftovers: list[Path] = []
    first_error: OSError | None = None
    for file in partition.files:
        if file == target:
            continue
        try:
            file.unlink(missing_ok=True)
        except OSError as exc:
            leftovers.append(file)
            first_error = first_error or exc
    if first_error is not None:
        raise LeftoverCleanupError(leftovers, first_error)
    return "changed"
