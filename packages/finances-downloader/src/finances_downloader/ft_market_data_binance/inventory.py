from pathlib import Path

import polars as pl
import structlog

logger = structlog.get_logger(__name__)


def inventory_local_parquet(parquet_dir: Path, symbol: str, timeframe: str, market_type: str) -> list[tuple[int, int]]:
    """
    Escanea particiones Hive existentes y retorna lista de rangos (min_open_time_ms, max_open_time_ms)
    por partición año/mes. Usa scan_parquet para leer solo metadata (statistics) sin cargar datos.
    """
    base_path = parquet_dir / "binance" / market_type / symbol.upper() / f"timeframe={timeframe}"
    if not base_path.exists():
        return []

    ranges = []
    for year_dir in base_path.iterdir():
        if not year_dir.is_dir() or not year_dir.name.startswith("year="):
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir() or not month_dir.name.startswith("month="):
                continue
            # Buscar archivos parquet en la partición
            parquet_files = list(month_dir.glob("*.parquet"))
            if not parquet_files:
                continue
            try:
                # Leer solo statistics de open_time_ms (min/max) sin cargar datos
                lf = pl.scan_parquet(parquet_files)
                stats = lf.select(
                    pl.col("open_time_ms").min().alias("min_ts"),
                    pl.col("open_time_ms").max().alias("max_ts"),
                ).collect()
                if stats.height > 0:
                    min_ts = stats["min_ts"][0]
                    max_ts = stats["max_ts"][0]
                    if min_ts is not None and max_ts is not None:
                        ranges.append((int(min_ts), int(max_ts)))
            except Exception as e:
                logger.warning("inventory_scan_failed", path=str(month_dir), error=str(e))

    # Ordenar por start
    ranges.sort(key=lambda x: x[0])
    return ranges


def compute_gaps(requested_start: int, requested_end: int, local_ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """
    Calcula gaps (huecos) entre el rango solicitado y los rangos locales existentes.
    Algoritmo: merge intervals + complemento.
    """
    if not local_ranges:
        return [(requested_start, requested_end)]

    # Merge overlapping/adjacent intervals
    merged: list[list[int]] = []
    for start, end in sorted(local_ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    gaps = []
    current = requested_start

    for start, end in merged:
        if current < start:
            gaps.append((current, min(start, requested_end)))
        current = max(current, end)
        if current >= requested_end:
            break

    if current < requested_end:
        gaps.append((current, requested_end))

    return gaps
