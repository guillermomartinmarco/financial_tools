"""Inventario local de particiones Hive y cálculo de huecos (gaps).

Lee el layout de :mod:`finances_core.marketdata.hive_writer`::

    {parquet_dir}/{exchange}/market_type={m}/symbol={S}/timeframe={tf}/year=YYYY/month=MM/data.parquet

``market_type="*"`` (:data:`ANY_MARKET_TYPE`) considera cualquier directorio
``market_type=...`` (yfinance detecta el tipo de instrumento en la respuesta).
El símbolo se usa tal cual: normalizarlo (p. ej. a mayúsculas) es tarea del llamador.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import structlog

logger = structlog.get_logger(__name__)

#: Comodín para inventariar todos los ``market_type`` de un símbolo.
ANY_MARKET_TYPE = "*"


def partition_base_dir(
    parquet_dir: Path, exchange: str, symbol: str, timeframe: str, market_type: str
) -> Path:
    """Directorio Hive de un par ``(market_type, symbol, timeframe)`` bajo ``exchange``."""
    return (
        parquet_dir
        / exchange
        / f"market_type={market_type}"
        / f"symbol={symbol}"
        / f"timeframe={timeframe}"
    )


def _base_dirs(
    parquet_dir: Path, exchange: str, symbol: str, timeframe: str, market_type: str
) -> list[Path]:
    if market_type != ANY_MARKET_TYPE:
        return [partition_base_dir(parquet_dir, exchange, symbol, timeframe, market_type)]
    root = parquet_dir / exchange
    if not root.is_dir():
        return []
    # Se itera sobre los directorios reales (no glob con el símbolo, que puede tener
    # metacaracteres como ``[``), y se compone la ruta exacta del símbolo.
    return [
        market_dir / f"symbol={symbol}" / f"timeframe={timeframe}"
        for market_dir in sorted(root.glob("market_type=*"))
        if market_dir.is_dir()
    ]


def inventory_local_parquet(
    parquet_dir: Path, symbol: str, timeframe: str, market_type: str, *, exchange: str
) -> list[tuple[int, int]]:
    """Rangos ``(min_open_time_ms, max_open_time_ms)`` por partición año/mes existente.

    Una sola consulta *lazy* (``scan_parquet`` + ``group_by`` sobre la columna de
    ruta) proyecta únicamente ``open_time_ms``; las particiones ilegibles se
    omiten con un *warning*.
    """
    files: list[Path] = []
    for base in _base_dirs(parquet_dir, exchange, symbol, timeframe, market_type):
        if base.is_dir():
            files.extend(
                p for p in base.glob("year=*/month=*/*.parquet") if p.is_file() and _readable(p)
            )
    if not files:
        return []

    stats = (
        pl.scan_parquet(sorted(files), include_file_paths="_file")
        .group_by("_file")
        .agg(
            pl.col("open_time_ms").min().alias("min_ts"),
            pl.col("open_time_ms").max().alias("max_ts"),
        )
        .drop_nulls()
        .collect()
    )
    ranges = [(int(lo), int(hi)) for lo, hi in stats.select("min_ts", "max_ts").iter_rows()]
    ranges.sort()
    return ranges


def _readable(path: Path) -> bool:
    """Valida el *footer* Parquet sin leer datos (descarta archivos corruptos)."""
    try:
        pl.read_parquet_schema(path)
    except Exception as exc:  # cualquier fallo de lectura invalida la partición
        logger.warning("inventory_scan_failed", path=str(path), error=str(exc))
        return False
    return True


def coverage_ranges(
    local_ranges: list[tuple[int, int]], timeframe_ms: int
) -> list[tuple[int, int]]:
    """Cada partición cubre desde su primera vela hasta el cierre de la última (+1 timeframe)."""
    return [(lo, hi + timeframe_ms) for lo, hi in local_ranges]


def compute_gaps(
    requested_start: int, requested_end: int, local_ranges: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Huecos de ``[requested_start, requested_end]`` no cubiertos por ``local_ranges``.

    Algoritmo: ordenar, fusionar intervalos solapados/adyacentes y emitir el complemento.
    """
    if not local_ranges:
        return [(requested_start, requested_end)]

    merged: list[list[int]] = []
    for start, end in sorted(local_ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    gaps: list[tuple[int, int]] = []
    current = requested_start
    for start, end in merged:
        if current >= requested_end:
            break
        if current < start:
            gaps.append((current, min(start, requested_end)))
        current = max(current, end)

    if current < requested_end:
        gaps.append((current, requested_end))
    return gaps
