"""Almacén de resultados de backtest (RF-7, RF-8, RF-9, A11, plan §1.5 y §3.4).

Layout (claves Hive como los demás almacenes, A11)::

    {FT_RESULTS_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/{run_id}/
        trades.parquet  equity.parquet  summary.json  manifest.json

``manifest.json`` es el **marcador de completitud**: un directorio sin un manifiesto
válido (que no valida, con otro ``run_id`` o en otra serie) no es un resultado.

Atomicidad (RF-8, RF-9, Art. 12, Art. 16): :func:`write_run` escribe los cuatro archivos
en un directorio hermano propio ``{run_id}.tmp-{uuid}`` (el manifiesto el último) y lo
instala con un único ``os.rename`` de directorio, así que el destino solo existe completo.
``os.rename`` falla si el destino ya existe (en Windows siempre; en POSIX si no está vacío),
lo que resuelve las carreras sin bloqueos:

- sin ``force``, el escritor que llega segundo descarta su temporal y devuelve el existente;
- con ``force``, el resultado anterior se aparta a ``{run_id}.old-{uuid}``, se instala el
  nuevo y se borra el apartado; si otro escritor instala antes, su contenido es idéntico
  (determinismo) y se devuelve ese. Si la instalación falla, el apartado vuelve a su sitio
  (nunca faltan a la vez viejo y nuevo); si el proceso muere entre ambos ``rename``,
  ``find_run``/``write_run`` (y ``list_runs`` pasado :data:`ASIDE_RECOVERY_GRACE_S`)
  restauran el apartado válido. Durante el intercambio un lector puede no encontrar la
  corrida, nunca verla a medias;
- en Windows renombrar un directorio con un archivo abierto (un lector) da
  ``PermissionError``: se reintenta con espera acotada.

Los temporales y apartados huérfanos (solo los nombres de este almacén; ``FT_RESULTS_DIR``
es compartido) con más de :data:`ORPHAN_MAX_AGE_S` se limpian al listar y al escribir.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import structlog
from pydantic import ValidationError

from finances_core.backtest.frames import validate_engine_output
from finances_core.marketdata.hive_writer import atomic_write
from finances_core.marketdata.inventory import partition_base_dir
from finances_core.schemas.backtest import (
    EquityPage,
    RunListItem,
    RunManifest,
    RunsPage,
    RunSummary,
    TradesPage,
)
from finances_core.schemas.data_viewer import DatasetIdentifier

logger = structlog.get_logger(__name__)

TRADES_FILE = "trades.parquet"
EQUITY_FILE = "equity.parquet"
SUMMARY_FILE = "summary.json"
MANIFEST_FILE = "manifest.json"

#: Advertencia de RF-7 cuando ``cagr`` es ``null`` porque la anualización desborda.
CAGR_NOT_FINITE = "cagr_not_finite"
#: Antigüedad a partir de la cual un temporal o apartado se considera huérfano (plan §3.4).
ORPHAN_MAX_AGE_S = 24 * 3600.0
#: Límite de página de ``trades``/``equity``/corridas (RF-9: ``limit`` 1..10000).
PAGE_LIMIT_MAX = 10_000

#: Un ``.old-*`` sin resultado más antiguo que esto se restaura al listar: ya no puede ser
#: de un ``force`` en curso (el intercambio dura milisegundos más reintentos de ~2 s).
ASIDE_RECOVERY_GRACE_S = 60.0

_RUN_ID_LEN = 64
_RUN_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ORPHAN_RE = re.compile(r"^[0-9a-f]{64}\.(?:tmp|old)-[0-9a-f]{32}$")
_CURSOR_RE = re.compile(r"^(-?[0-9]+):([0-9a-f]{64})$")
_RUN_GLOB = "*/market_type=*/symbol=*/timeframe=*/*"
#: Reintentos de un ``rename`` bloqueado por un archivo abierto (Windows) y de la instalación.
_RENAME_ATTEMPTS = 100
_RENAME_WAIT_S = 0.02
_INSTALL_ATTEMPTS = 20
_REMOVE_ATTEMPTS = 25
_DESTINATION_EXISTS = frozenset({errno.EEXIST, errno.ENOTEMPTY})


@dataclass(frozen=True)
class StoredRun:
    """Corrida completa del almacén; ``written`` es ``True`` solo si esta llamada la instaló."""

    path: Path
    manifest: RunManifest
    summary: RunSummary
    written: bool = False


@dataclass(frozen=True)
class _Tables:
    trades: pl.DataFrame
    equity: pl.DataFrame


# --------------------------------------------------------------------- rutas
def run_dir(results_dir: Path, dataset: DatasetIdentifier, run_id: str) -> Path:
    """``{results_dir}/{exchange}/market_type=…/symbol=…/timeframe=…/{run_id}`` (RF-9, A11)."""
    base = partition_base_dir(
        Path(results_dir), dataset.exchange, dataset.symbol, dataset.timeframe, dataset.market_type
    )
    return base / run_id


def _expected_parts(manifest: RunManifest) -> tuple[str, ...]:
    ds = manifest.request.dataset
    return (
        ds.exchange,
        f"market_type={ds.market_type}",
        f"symbol={ds.symbol}",
        f"timeframe={ds.timeframe}",
        manifest.run_id,
    )


# ------------------------------------------------------------------ lectura
def read_manifest(path: Path) -> RunManifest | None:
    """Manifiesto de la corrida en ``path`` o ``None`` si no es un resultado válido.

    No es resultado: sin ``manifest.json``, que no valida como :class:`RunManifest`, cuyo
    ``run_id`` no es el nombre del directorio o cuya serie no es la de su ubicación.
    """
    path = Path(path)
    try:
        text = (path / MANIFEST_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        manifest = RunManifest.model_validate_json(text)
    except ValidationError:
        logger.debug("run_manifest_invalid", path=str(path))
        return None
    if path.parts[-5:] != _expected_parts(manifest):
        logger.debug("run_manifest_misplaced", path=str(path), run_id=manifest.run_id)
        return None
    return manifest


def read_summary(path: Path) -> RunSummary:
    """``summary.json`` de la corrida en ``path``.

    Raises:
        OSError: SI el archivo no existe o no se puede leer.
        ValidationError: SI no valida como :class:`RunSummary`.
    """
    return RunSummary.model_validate_json((Path(path) / SUMMARY_FILE).read_text(encoding="utf-8"))


def _load(path: Path) -> StoredRun | None:
    manifest = read_manifest(path)
    if manifest is None:
        return None
    try:
        summary = read_summary(path)
    except (OSError, ValidationError):
        return None  # p. ej. apartada por un reemplazo con ``force`` entre ambas lecturas
    return StoredRun(path=path, manifest=manifest, summary=summary)


def find_run(results_dir: Path, run_id: str) -> StoredRun | None:
    """Corrida ``run_id`` en cualquier serie de ``results_dir`` o ``None``.

    Recorre las series (O(nº de series), suficiente en el MVP; plan §1.5). Un ``run_id``
    que no sea SHA-256 hex en minúsculas no se busca (nunca entra en un patrón ``glob``).
    """
    if not _RUN_ID_RE.fullmatch(run_id):
        return None
    root = Path(results_dir)
    for candidate in sorted(root.glob(f"*/market_type=*/symbol=*/timeframe=*/{run_id}")):
        stored = _load(candidate)
        if stored is not None:
            return stored
    # Sin destino pero con un apartado válido: proceso muerto entre los dos ``rename`` de
    # ``force`` (o reemplazo en curso, de contenido idéntico): se restaura y se sirve.
    for aside in sorted(root.glob(f"*/market_type=*/symbol=*/timeframe=*/{run_id}.old-*")):
        final = aside.with_name(run_id)
        _recover_aside(final)
        stored = _load(final)
        if stored is not None:
            return stored
    return None


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= PAGE_LIMIT_MAX:
        msg = f"limit debe estar en 1..{PAGE_LIMIT_MAX} (recibido {limit})"
        raise ValueError(msg)


def _read_page(
    path: Path, file_name: str, column: str, after_ms: int | None, limit: int
) -> tuple[list[dict[str, Any]], int | None]:
    _check_limit(limit)
    # Se lee desde un descriptor propio y se cierra enseguida: no deja *mappings* abiertos
    # que impidan a un escritor con ``force`` apartar el directorio en Windows.
    with (Path(path) / file_name).open("rb") as handle:
        frame = pl.read_parquet(handle)
    if after_ms is not None:
        frame = frame.filter(pl.col(column) > after_ms)
    page = frame.head(limit)
    next_after = int(page.get_column(column)[-1]) if frame.height > limit else None
    return page.to_dicts(), next_after


def read_trades_page(path: Path, *, after_ms: int | None = None, limit: int = 1000) -> TradesPage:
    """Página de ``trades`` con ``entry_time_ms > after_ms`` (cursor exclusivo, RF-9).

    Raises:
        ValueError: SI ``limit`` no está en ``1..10000``.
        OSError: SI la corrida ya no existe en ``path``.
    """
    items, next_after = _read_page(path, TRADES_FILE, "entry_time_ms", after_ms, limit)
    return TradesPage(items=items, count=len(items), next_after_ms=next_after)


def read_equity_page(path: Path, *, after_ms: int | None = None, limit: int = 1000) -> EquityPage:
    """Página de ``equity`` con ``open_time_ms > after_ms`` (cursor exclusivo, RF-9).

    Raises:
        ValueError: SI ``limit`` no está en ``1..10000``.
        OSError: SI la corrida ya no existe en ``path``.
    """
    items, next_after = _read_page(path, EQUITY_FILE, "open_time_ms", after_ms, limit)
    return EquityPage(items=items, count=len(items), next_after_ms=next_after)


def _parse_cursor(cursor: str) -> tuple[int, str]:
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        msg = f"cursor inválido: {cursor!r}"
        raise ValueError(msg)
    return int(match.group(1)), match.group(2)


def _sort_key(manifest: RunManifest) -> tuple[int, str]:
    return -manifest.created_at_ms, manifest.run_id


def _matches(manifest: RunManifest, filters: dict[str, str | None]) -> bool:
    ds = manifest.request.dataset
    actual = {
        "exchange": ds.exchange,
        "market_type": ds.market_type,
        "symbol": ds.symbol,
        "timeframe": ds.timeframe,
        "strategy_id": manifest.strategy.strategy_id,
        "engine": manifest.engine,
    }
    return all(value is None or actual[key] == value for key, value in filters.items())


def _iter_manifests(root: Path) -> Iterator[tuple[Path, RunManifest]]:
    for candidate in root.glob(_RUN_GLOB):
        if _RUN_ID_RE.fullmatch(candidate.name):
            manifest = read_manifest(candidate)
            if manifest is not None:
                yield candidate, manifest


def list_runs(  # noqa: PLR0913 - filtros de RF-9 con nombre
    results_dir: Path,
    *,
    exchange: str | None = None,
    market_type: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    strategy_id: str | None = None,
    engine: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> RunsPage:
    """Corridas válidas que cumplen los filtros, de la más reciente a la más antigua (RF-9).

    Orden ``(created_at_ms desc, run_id asc)``; ``next_cursor`` (``"{created_at_ms}:{run_id}"``)
    es la última corrida de la página y la siguiente empieza estrictamente después. Limpia
    antes los temporales huérfanos (plan §3.4).

    Raises:
        ValueError: SI ``limit`` no está en ``1..10000`` o ``cursor`` está mal formado.
    """
    _check_limit(limit)
    after = _parse_cursor(cursor) if cursor is not None else None
    root = Path(results_dir)
    cleanup_orphans(root)
    filters = {
        "exchange": exchange,
        "market_type": market_type,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "engine": engine,
    }
    selected = sorted(
        (
            (path, manifest)
            for path, manifest in _iter_manifests(root)
            if _matches(manifest, filters)
            and (after is None or _sort_key(manifest) > (-after[0], after[1]))
        ),
        key=lambda pair: _sort_key(pair[1]),
    )
    page = selected[:limit]
    items: list[RunListItem] = []
    for path, manifest in page:
        try:
            summary = read_summary(path)
        except (OSError, ValidationError):
            continue  # apartada por un reemplazo concurrente
        items.append(
            RunListItem(
                run_id=manifest.run_id,
                engine=manifest.engine,
                strategy_id=manifest.strategy.strategy_id,
                strategy_version=manifest.strategy.version,
                kind=manifest.strategy.kind,
                dataset=manifest.request.dataset,
                created_at_ms=manifest.created_at_ms,
                summary=summary,
            )
        )
    next_cursor = None
    if len(selected) > limit:
        last = page[-1][1]
        next_cursor = f"{last.created_at_ms}:{last.run_id}"
    return RunsPage(items=items, count=len(items), next_cursor=next_cursor)


# ----------------------------------------------------------- limpieza huérfanos
def _remove_tree(path: Path) -> bool:
    """Borra ``path`` (best-effort) reintentando si un lector tiene un archivo abierto.

    En Windows un archivo abierto (p. ej. un ``manifest.json`` que otro hilo está
    validando) no se puede borrar; tras los reintentos queda para la limpieza por edad.
    """
    for attempt in range(_REMOVE_ATTEMPTS):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        if attempt < _REMOVE_ATTEMPTS - 1:
            time.sleep(_RENAME_WAIT_S)
    return False


def _aside_is_valid(aside: Path) -> bool:
    """El apartado contiene un manifiesto válido de su ``run_id`` (resultado completo)."""
    try:
        manifest = RunManifest.model_validate_json(
            (aside / MANIFEST_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError):
        return False
    return manifest.run_id == aside.name[:_RUN_ID_LEN]


def _recover_aside(final: Path) -> bool:
    """Restaura en ``final`` un apartado ``{run_id}.old-*`` válido si ``final`` no existe.

    Recupera el estado que deja un proceso muerto entre apartar el resultado anterior e
    instalar el nuevo (``force``). Best-effort: si otro lo restaura o instala antes, no
    hace nada.
    """
    if final.exists():
        return False
    try:
        asides = sorted(final.parent.glob(f"{final.name}.old-*"))
    except OSError:
        return False
    for aside in asides:
        if not (_ORPHAN_RE.fullmatch(aside.name) and _aside_is_valid(aside)):
            continue
        try:
            _rename(aside, final)
        except OSError:
            continue
        logger.warning("backtest_aside_restored", path=str(final), aside=aside.name)
        return True
    return False


def _cleanup_series(series_dir: Path, cutoff: float, *, recover_before: float) -> list[Path]:
    """Limpia los huérfanos de una serie; un apartado válido sin destino nunca se borra.

    Un apartado sin destino puede pertenecer a un ``force`` en curso: solo se restaura si es
    anterior a ``recover_before`` (su ``mtime`` se fija al apartarlo).
    """
    removed: list[Path] = []
    try:
        entries = list(series_dir.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if not _ORPHAN_RE.fullmatch(entry.name):
            continue
        try:
            if not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        final = series_dir / entry.name[:_RUN_ID_LEN]
        if ".old-" in entry.name and not final.exists() and _aside_is_valid(entry):
            if mtime < recover_before:
                _recover_aside(final)
            continue
        if mtime < cutoff and _remove_tree(entry):
            removed.append(entry)
    return removed


def cleanup_orphans(
    results_dir: Path, *, max_age_s: float = ORPHAN_MAX_AGE_S, now: float | None = None
) -> list[Path]:
    """Borra los ``{run_id}.tmp-*``/``{run_id}.old-*`` con más de ``max_age_s`` (best-effort).

    Solo toca nombres de este almacén; los más recientes pueden pertenecer a una escritura
    en curso y se conservan. Un ``.old-*`` cuyo resultado no existe nunca se borra: si es
    anterior a :data:`ASIDE_RECOVERY_GRACE_S` se restaura (proceso muerto durante ``force``).
    """
    current = time.time() if now is None else now
    removed: list[Path] = []
    for series_dir in sorted(Path(results_dir).glob("*/market_type=*/symbol=*/timeframe=*")):
        removed.extend(
            _cleanup_series(
                series_dir, current - max_age_s, recover_before=current - ASIDE_RECOVERY_GRACE_S
            )
        )
    if removed:
        logger.info("backtest_orphans_removed", count=len(removed))
    return sorted(removed)


# ----------------------------------------------------------------- escritura
def _fsync(path: Path) -> None:
    """``fsync`` best-effort (los directorios no admiten ``fsync`` en Windows)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_parquet(frame: pl.DataFrame, path: Path) -> None:
    # ``atomic_write`` fija la compresión del almacén (``zstd`` nivel 3, estadísticas) y no
    # escribe metadatos dependientes del tiempo: reescribir da los mismos bytes (RF-9).
    atomic_write(frame, path)
    _fsync(path)


def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _rename(src: Path, dst: Path) -> None:
    """``os.rename`` reintentando el ``PermissionError`` transitorio de Windows.

    Raises:
        FileExistsError: SI ``dst`` ya existe (también ``ENOTEMPTY`` de POSIX).
        FileNotFoundError: SI ``src`` ya no existe.
        PermissionError: SI el bloqueo persiste tras los reintentos.
    """
    for attempt in range(_RENAME_ATTEMPTS):
        try:
            os.rename(src, dst)  # noqa: PTH104 - se parchea en los tests como ``os.rename``
        except PermissionError:
            if attempt == _RENAME_ATTEMPTS - 1:
                raise
            time.sleep(_RENAME_WAIT_S)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno in _DESTINATION_EXISTS:
                raise FileExistsError(exc.errno, exc.strerror, str(dst)) from exc
            raise
        else:
            return


def _finalize(manifest: RunManifest, summary: RunSummary) -> tuple[RunManifest, RunSummary]:
    """Advertencia ``cagr_not_finite`` y rango/advertencias del manifiesto en el resumen."""
    warnings = list(manifest.warnings)
    if summary.cagr is None and CAGR_NOT_FINITE not in warnings:
        warnings.append(CAGR_NOT_FINITE)
    manifest = manifest.model_copy(update={"warnings": warnings})
    summary = summary.model_copy(
        update={"start_ms": manifest.start_ms, "end_ms": manifest.end_ms, "warnings": warnings}
    )
    return manifest, summary


def _check_inputs(
    manifest: RunManifest, summary: RunSummary, trades: pl.DataFrame, equity: pl.DataFrame
) -> None:
    if not _RUN_ID_RE.fullmatch(manifest.run_id):
        msg = f"run_id inválido: {manifest.run_id!r}"
        raise ValueError(msg)
    validate_engine_output(_Tables(trades, equity), manifest.n_bars)
    if summary.n_bars != manifest.n_bars:
        msg = f"summary.n_bars ({summary.n_bars}) != manifest.n_bars ({manifest.n_bars})"
        raise ValueError(msg)
    times = equity.get_column("open_time_ms")
    if equity.height and (times[0], times[-1]) != (manifest.start_ms, manifest.end_ms):
        msg = (
            f"rango de equity ({times[0]}, {times[-1]}) != rango efectivo del manifiesto "
            f"({manifest.start_ms}, {manifest.end_ms})"
        )
        raise ValueError(msg)


def _move_aside(final: Path) -> Path | None:
    """Aparta ``final`` a ``{run_id}.old-{uuid}`` con ``mtime`` actual; ``None`` si no existe.

    NTFS conserva el ``mtime`` al renombrar: sin ``utime`` el apartado de un resultado de
    hace más de 24 h parecería huérfano (o recuperable) para una limpieza concurrente. Se
    fija **antes** de renombrar, para que el apartado nazca ya con ``mtime`` actual.
    """
    if not final.exists():
        return None
    try:
        os.utime(final)
    except FileNotFoundError:
        return None  # otro escritor lo apartó antes
    except OSError:
        logger.warning("backtest_aside_utime_failed", path=str(final))
    aside = final.with_name(f"{final.name}.old-{uuid.uuid4().hex}")
    try:
        _rename(final, aside)
    except FileNotFoundError:
        return None
    return aside


def _discard_aside(aside: Path | None) -> None:
    if aside is not None and not _remove_tree(aside):
        logger.warning("backtest_old_run_not_removed", path=str(aside))


def _restore_aside(aside: Path | None, final: Path) -> None:
    """Devuelve el apartado a ``final`` tras un fallo: nunca faltan a la vez viejo y nuevo."""
    if aside is None:
        return
    try:
        _rename(aside, final)
    except FileExistsError:
        _discard_aside(aside)  # otro escritor instaló un resultado completo
    except OSError:
        # Se conserva: ``find_run``/``write_run``/``list_runs`` lo restaurarán.
        logger.exception("backtest_aside_restore_failed", path=str(aside))


def _install_without_force(tmp: Path, final: Path, stored: StoredRun) -> StoredRun | None:
    """Instala ``tmp`` sin apartar nunca un resultado completo; ``None`` para reintentar.

    Primero se intenta el ``rename``: si ``final`` ya existe y es un resultado completo (otro
    escritor lo instaló, quizá tras la comprobación inicial), se devuelve sin escribir.
    Apartar antes de comprobar dejaría ``final`` ausente un instante y otro escritor sin
    ``force`` podría instalar el suyo: dos ``written = True``. Solo un ``final`` incompleto
    (sin ``summary.json`` válido, p. ej. corrupto o de un esquema anterior) se aparta.
    """
    try:
        _rename(tmp, final)
    except FileExistsError:
        existing = _load(final)
        if existing is not None:
            return existing
    else:
        return stored
    aside = _move_aside(final)
    try:
        _rename(tmp, final)
    except FileExistsError:
        _discard_aside(aside)  # otro escritor instaló entre medias: se reintenta la carga
        return None
    except BaseException:
        _restore_aside(aside, final)
        raise
    _discard_aside(aside)
    return stored


def _install(tmp: Path, final: Path, stored: StoredRun, *, force: bool) -> StoredRun:
    for _ in range(_INSTALL_ATTEMPTS):
        if not force:
            installed = _install_without_force(tmp, final, stored)
            if installed is not None:
                return installed
            continue
        aside = _move_aside(final)  # resultado a reemplazar
        try:
            _rename(tmp, final)
        except FileExistsError:
            # Otro escritor instaló (o restauró) entre medias un resultado completo y, por
            # determinismo, idéntico: se acepta en vez de reemplazarlo otra vez.
            _discard_aside(aside)
            force = False
            continue
        except BaseException:
            _restore_aside(aside, final)
            raise
        _discard_aside(aside)  # solo tras instalar el nuevo
        return stored
    msg = f"no se pudo instalar {final} tras {_INSTALL_ATTEMPTS} intentos"
    raise OSError(msg)


def write_run(  # noqa: PLR0913 - partes de una corrida, con nombre
    results_dir: Path,
    *,
    manifest: RunManifest,
    summary: RunSummary,
    trades: pl.DataFrame,
    equity: pl.DataFrame,
    force: bool = False,
) -> StoredRun:
    """Persiste una corrida de forma atómica en el layout de RF-9.

    ``summary.json`` recibe el rango efectivo y las advertencias del manifiesto (única
    fuente) y la advertencia ``cagr_not_finite`` si ``cagr`` es ``null`` (RF-7). SI ya existe
    un resultado válido con ese ``run_id`` y no ``force``, ENTONCES se devuelve sin escribir
    (``written = False``); con ``force`` se reemplaza atómicamente.

    Raises:
        OutputOutOfRangeError: SI ``trades``/``equity`` no son canónicas (RF-6).
        ValueError: SI el ``run_id``, ``n_bars`` o el rango no son coherentes con el
            manifiesto. No se escribe nada.
        OSError: ante errores de disco; nunca queda un resultado a medias.
    """
    _check_inputs(manifest, summary, trades, equity)
    manifest, summary = _finalize(manifest, summary)
    final = run_dir(results_dir, manifest.request.dataset, manifest.run_id)
    _recover_aside(final)
    if not force and (existing := _load(final)) is not None:
        return existing
    final.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    _cleanup_series(
        final.parent, now - ORPHAN_MAX_AGE_S, recover_before=now - ASIDE_RECOVERY_GRACE_S
    )
    tmp = final.with_name(f"{final.name}.tmp-{uuid.uuid4().hex}")
    tmp.mkdir()
    try:
        _write_parquet(trades, tmp / TRADES_FILE)
        _write_parquet(equity, tmp / EQUITY_FILE)
        _write_text(tmp / SUMMARY_FILE, summary.model_dump_json(indent=2) + "\n")
        _write_text(tmp / MANIFEST_FILE, manifest.model_dump_json(indent=2) + "\n")  # el último
        stored = _install(
            tmp, final, StoredRun(final, manifest, summary, written=True), force=force
        )
    finally:
        if tmp.exists():
            _remove_tree(tmp)
    logger.debug(
        "backtest_run_stored",
        run_id=manifest.run_id,
        path=str(final),
        written=stored.written,
        forced=force,
    )
    return stored
