"""Cálculo de *locks* y verificación de recetas (plan §3.5, RF-5, RF-6, RF-7).

La verificación es local (sin red): lee solo las particiones que solapan cada ítem y
compara el hash ``ftds-v1`` con el *lock*. Si difiere, clasifica la deriva con los
hashes por columna y por mes y con las tres sondas de ``close``. Vive en el núcleo para
que los tests ``real_data`` de cualquier paquete verifiquen sin importar ``ft_datasets``.
"""

from __future__ import annotations

import math
import statistics
import struct
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import structlog

from finances_core.datasets import store
from finances_core.datasets.hashing import ItemHasher, ProbeValue
from finances_core.schemas.datasets import (
    HASH_ALGORITHM,
    DatasetRecipe,
    Drift,
    ItemLock,
    ItemStatus,
    ItemVerification,
    Probe,
    RecipeItem,
    RecipeLock,
    VerifyReport,
    VerifyStatus,
)
from finances_core.schemas.market_data import TF_MS_MAP

logger = structlog.get_logger(__name__)

#: Columnas de precio cuyo cambio conjunto con cociente constante es ``SCALE_RESTATED``.
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
#: Tolerancia relativa de las sondas para ``SCALE_RESTATED`` (RF-6).
SCALE_REL_TOLERANCE = 1e-3
#: Primer lunes 00:00 UTC tras el epoch (las velas ``1w`` de Binance abren en lunes).
_FIRST_MONDAY_MS = 4 * 86_400_000
_WEEK_MS = TF_MS_MAP["1w"]

ACTION_MISMATCH = (
    "Re-materializar con `ft_datasets materialize <receta> --refetch` si se sospecha "
    "corrupción local; re-bloquear con `ft_datasets lock <receta> --update` (en un PR "
    "revisado) si el proveedor re-expresó el histórico."
)
ACTION_MISSING = "Materializar la receta con `ft_datasets materialize <receta>`."
ACTION_UNLOCKED = "Bloquear con `ft_datasets lock <receta>` y revisar el lock en un PR."


# --- Lock de un ítem -----------------------------------------------------------------------


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _aligned_count(start_ms: int, end_ms: int, step: int, offset: int = 0) -> int:
    """Instantes ``offset + k·step`` dentro de ``[start_ms, end_ms)``."""
    return _ceil_div(end_ms - offset, step) - _ceil_div(start_ms - offset, step)


def expected_candles(item: RecipeItem) -> int:
    """Velas esperadas en ``[start_ms, end_ms)`` según un calendario 24/7 (Binance).

    ``1M``: meses naturales; ``1w``: lunes 00:00 UTC; resto: múltiplos del timeframe
    desde el epoch.
    """
    if item.timeframe == "1M":
        start = datetime.fromtimestamp(item.start_ms / 1000, tz=UTC)
        end = datetime.fromtimestamp(item.end_ms / 1000, tz=UTC)
        first = start.year * 12 + start.month - 1 + (start.day > 1 or start.hour > 0)
        last = end.year * 12 + end.month - 1 + (end.day > 1 or end.hour > 0)
        return max(0, last - first)
    if item.timeframe == "1w":
        return _aligned_count(item.start_ms, item.end_ms, _WEEK_MS, _FIRST_MONDAY_MS)
    return _aligned_count(item.start_ms, item.end_ms, TF_MS_MAP[item.timeframe])


def _bits_to_float(bits: int) -> float:
    value: float = struct.unpack("<f", bits.to_bytes(4, "little"))[0]
    return value


def _probe(value: ProbeValue) -> Probe:
    if value.close_bits is None:
        return Probe(row=value.row, open_time_ms=value.open_time_ms, close_bits=None, close=None)
    close = _bits_to_float(value.close_bits)
    return Probe(
        row=value.row,
        open_time_ms=value.open_time_ms,
        close_bits=f"0x{value.close_bits:08x}",
        close=close if math.isfinite(close) else None,
    )


def compute_item_lock(datasets_dir: Path, item: RecipeItem) -> ItemLock | None:
    """Hashes ``ftds-v1`` del ítem desde el almacén; ``None`` si no hay filas (``MISSING``).

    Lee mes a mes (*streaming*, Art. 19); las sondas son las filas ``0``, ``n//2`` y ``n-1``.

    Raises:
        DatasetDataError: datos no canónicos en el almacén.
        OSError: error de IO.
    """
    rows = store.count_rows(datasets_dir, item)
    if rows == 0:
        return None
    strict, informational = store.item_columns(item)
    probe_rows = (0, rows // 2, rows - 1)
    hasher = ItemHasher(strict, informational, probe_rows)
    for _month, frame in store.scan_item(datasets_dir, item):
        hasher.update(frame)
    result = hasher.result()
    if result.row_count == 0 or result.first_open_time_ms is None:
        return None
    probes_by_row = {p.row: p for p in result.probes}
    return ItemLock(
        key=item.key,
        exchange=item.exchange,
        market_type=item.market_type,
        symbol=item.symbol,
        timeframe=item.timeframe,
        start_ms=item.start_ms,
        end_ms=item.end_ms,
        row_count=result.row_count,
        first_open_time_ms=result.first_open_time_ms,
        last_open_time_ms=result.last_open_time_ms or result.first_open_time_ms,
        strict_columns=list(strict),
        content_hash=result.content_hash,
        column_hashes=result.column_hashes,
        month_hashes=result.month_hashes,
        informational=result.informational,
        probes=[_probe(probes_by_row[r]) for r in probe_rows if r in probes_by_row],
        missing_candles=(
            expected_candles(item) - result.row_count if item.exchange == "binance" else None
        ),
    )


def build_recipe_lock(
    recipe: DatasetRecipe,
    item_locks: Sequence[ItemLock],
    *,
    locked_at: datetime,
    tool_versions: dict[str, str],
) -> RecipeLock:
    """Ensambla el *lock* de la receta (ítems en el orden de :meth:`DatasetRecipe.items`)."""
    by_key = {lock.key: lock for lock in item_locks}
    ordered = [by_key[item.key] for item in recipe.items() if item.key in by_key]
    return RecipeLock(
        lock_version=1,
        hash_algorithm=HASH_ALGORITHM,
        recipe=recipe.name,
        recipe_sha256=recipe.canonical_sha256(),
        locked_at=locked_at,
        tool_versions=tool_versions,
        items=ordered,
    )


# --- Clasificación de la deriva (RF-6) -----------------------------------------------------


def _scale_factor(current: ItemLock, expected: ItemLock) -> float | None:
    """Mediana de ``close_actual / close_bloqueado`` si las 3 sondas coinciden (tol. 1e-3)."""
    if len(current.probes) != len(expected.probes) or not expected.probes:
        return None
    ratios: list[float] = []
    for now, then in zip(current.probes, expected.probes, strict=True):
        if (now.row, now.open_time_ms) != (then.row, then.open_time_ms):
            return None
        if now.close_bits is None or then.close_bits is None:
            return None
        a, b = _bits_to_float(int(now.close_bits, 16)), _bits_to_float(int(then.close_bits, 16))
        if not (math.isfinite(a) and math.isfinite(b)) or b == 0.0:
            return None
        ratios.append(a / b)
    factor = statistics.median(ratios)
    if factor == 0.0 or any(abs(r - factor) > SCALE_REL_TOLERANCE * abs(factor) for r in ratios):
        return None
    return factor


def classify_drift(current: ItemLock, expected: ItemLock) -> tuple[Drift, float | None]:
    """``ROWS_CHANGED`` / ``SCALE_RESTATED`` (con factor) / ``VALUES_CHANGED`` (RF-6)."""
    rows_changed = (
        current.row_count != expected.row_count
        or current.first_open_time_ms != expected.first_open_time_ms
        or current.last_open_time_ms != expected.last_open_time_ms
        or current.column_hashes.get("open_time_ms") != expected.column_hashes.get("open_time_ms")
    )
    if rows_changed:
        return "ROWS_CHANGED", None
    prices = [c for c in PRICE_COLUMNS if c in expected.column_hashes]
    all_prices_changed = bool(prices) and all(
        current.column_hashes.get(c) != expected.column_hashes[c] for c in prices
    )
    if all_prices_changed:
        factor = _scale_factor(current, expected)
        if factor is not None:
            return "SCALE_RESTATED", factor
    return "VALUES_CHANGED", None


def _changed_months(current: ItemLock, expected: ItemLock) -> list[str]:
    months = set(current.month_hashes) | set(expected.month_hashes)
    return sorted(m for m in months if current.month_hashes.get(m) != expected.month_hashes.get(m))


def _informational_warnings(current: ItemLock, expected: ItemLock) -> list[str]:
    columns = sorted(set(current.informational) | set(expected.informational))
    return [
        f"columna informativa {c} distinta del lock (re-expresión del proveedor; no es fallo)"
        for c in columns
        if current.informational.get(c) != expected.informational.get(c)
    ]


def compare_item(key: str, current: ItemLock | None, expected: ItemLock | None) -> ItemVerification:
    """Estado RF-5 de un ítem a partir de su *lock* actual y el bloqueado."""
    expected_rows = expected.row_count if expected else None
    expected_hash = expected.content_hash if expected else None
    if current is None:
        return ItemVerification(
            key=key,
            status="MISSING",
            row_count=0,
            expected_row_count=expected_rows,
            content_hash=None,
            expected_content_hash=expected_hash,
            action=ACTION_MISSING,
        )
    base = {
        "key": key,
        "row_count": current.row_count,
        "expected_row_count": expected_rows,
        "content_hash": current.content_hash,
        "expected_content_hash": expected_hash,
    }
    if expected is None:
        return ItemVerification.model_validate(
            base | {"status": "UNLOCKED", "action": ACTION_UNLOCKED}
        )
    warnings = _informational_warnings(current, expected)
    if current.content_hash == expected.content_hash:
        status: ItemStatus = "OK_WITH_WARNINGS" if warnings else "OK"
        return ItemVerification.model_validate(base | {"status": status, "warnings": warnings})
    drift, factor = classify_drift(current, expected)
    return ItemVerification.model_validate(
        base
        | {
            "status": "MISMATCH",
            "drift": drift,
            "scale_factor": factor,
            "columns_changed": [
                c
                for c in expected.strict_columns
                if current.column_hashes.get(c) != expected.column_hashes.get(c)
            ],
            "months_changed": _changed_months(current, expected),
            "warnings": warnings,
            "action": ACTION_MISMATCH,
        }
    )


def verify_item(
    datasets_dir: Path, item: RecipeItem, expected: ItemLock | None
) -> ItemVerification:
    """Verifica un ítem contra su entrada del *lock* (``None`` = sin bloquear)."""
    return compare_item(item.key, compute_item_lock(datasets_dir, item), expected)


def _global_status(recipe_changed: bool, items: Sequence[ItemVerification]) -> VerifyStatus:
    if recipe_changed:
        return "RECIPE_CHANGED"
    if any(i.status in {"MISSING", "MISMATCH"} for i in items):
        return "MISMATCH"
    if any(i.status == "UNLOCKED" for i in items):
        return "UNLOCKED"
    return "OK"


def verify_recipe(
    recipe: DatasetRecipe, lock: RecipeLock | None, datasets_dir: Path
) -> VerifyReport:
    """Verifica todos los ítems de ``recipe`` contra ``lock`` (RF-5); local y sin red.

    Raises:
        DatasetDataError: datos no canónicos en el almacén.
        OSError: error de IO.
    """
    started = time.perf_counter()
    expected = {i.key: i for i in lock.items} if lock else {}
    recipe_changed = lock is not None and lock.recipe_sha256 != recipe.canonical_sha256()
    items = [verify_item(datasets_dir, item, expected.get(item.key)) for item in recipe.items()]
    report = VerifyReport(
        recipe=recipe.name,
        status=_global_status(recipe_changed, items),
        recipe_changed=recipe_changed,
        items=items,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    logger.info(
        "dataset_recipe_verified",
        recipe=recipe.name,
        status=report.status,
        items=len(items),
        duration_ms=report.duration_ms,
    )
    return report
