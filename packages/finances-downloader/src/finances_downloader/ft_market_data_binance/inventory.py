"""Inventario local (RF-1 paso 2) y cálculo de huecos (paso 3).

Adaptador fino sobre :mod:`finances_core.marketdata.inventory` para el layout::

    {parquet_dir}/binance/market_type={m}/symbol={S}/timeframe={tf}/year=YYYY/month=MM/data.parquet

Binance normaliza los símbolos a mayúsculas.
"""

from __future__ import annotations

from pathlib import Path

from finances_core.marketdata import inventory as core_inventory
from finances_core.marketdata.inventory import compute_gaps

EXCHANGE = "binance"

__all__ = ["compute_gaps", "inventory_local_parquet", "partition_base_dir"]


def partition_base_dir(parquet_dir: Path, symbol: str, timeframe: str, market_type: str) -> Path:
    """Directorio Hive de un par ``(market_type, symbol, timeframe)``."""
    return core_inventory.partition_base_dir(
        parquet_dir, EXCHANGE, symbol.upper(), timeframe, market_type
    )


def inventory_local_parquet(
    parquet_dir: Path, symbol: str, timeframe: str, market_type: str
) -> list[tuple[int, int]]:
    """Rangos ``(min_open_time_ms, max_open_time_ms)`` por partición año/mes existente."""
    return core_inventory.inventory_local_parquet(
        parquet_dir, symbol.upper(), timeframe, market_type, exchange=EXCHANGE
    )
