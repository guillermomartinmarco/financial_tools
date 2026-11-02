"""Layout Hive de velas bajo ``FT_PARQUET_DIR`` compartido por las herramientas (Art. 4).

Layout (spec ``ft_market_data_binance`` RF-4, Hive completo)::

    {PARQUET_DIR}/{exchange}/market_type=X/symbol=Y/timeframe=Z/year=YYYY/month=MM/data.parquet

Los archivos ``data.parquet`` no contienen las columnas de partición; la identidad del
dataset sale exclusivamente de la ruta. Los árboles sin ese layout de velas (p. ej.
``yfinance_events/symbol=Y/events.parquet``) se ignoran en silencio. Solo trata rutas:
no lee ni escribe filas.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import structlog

from finances_core.schemas.data_viewer import DatasetIdentifier

logger = structlog.get_logger(__name__)

#: Valores válidos de ``market_type`` según el contrato ``DatasetIdentifier``.
MARKET_TYPES: tuple[str, ...] = get_args(DatasetIdentifier.model_fields["market_type"].annotation)


def hive_value(name: str, key: str) -> str | None:
    """Devuelve ``valor`` si ``name`` es ``key=valor`` (partición Hive), si no ``None``."""
    prefix = f"{key}="
    if name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix) :]
    return None


def hive_children(parent: Path, key: str) -> list[tuple[str, Path]]:
    """Subdirectorios ``key=valor`` de ``parent`` como ``(valor, ruta)``, en orden estable."""
    if not parent.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for child in sorted(parent.iterdir()):
        value = hive_value(child.name, key) if child.is_dir() else None
        if value is not None:
            out.append((value, child))
    return out


def discover_datasets(parquet_dir: Path) -> list[tuple[DatasetIdentifier, Path]]:
    """Recorre el árbol Hive y devuelve (identificador, raíz del dataset)."""
    found: list[tuple[DatasetIdentifier, Path]] = []
    if not parquet_dir.is_dir():
        return found
    for exchange_dir in sorted(p for p in parquet_dir.iterdir() if p.is_dir()):
        for market_type, market_dir in hive_children(exchange_dir, "market_type"):
            if market_type not in MARKET_TYPES:
                logger.warning("unknown_market_type_skipped", path=str(market_dir))
                continue
            for symbol, symbol_dir in hive_children(market_dir, "symbol"):
                for timeframe, tf_dir in hive_children(symbol_dir, "timeframe"):
                    ident = DatasetIdentifier.model_validate(
                        {
                            "exchange": exchange_dir.name,
                            "market_type": market_type,
                            "symbol": symbol,
                            "timeframe": timeframe,
                        }
                    )
                    found.append((ident, tf_dir))
    return found


def dataset_files(dataset_path: Path) -> list[Path]:
    """Archivos Parquet de un dataset (``year=*/month=*/*.parquet``), en orden estable."""
    return sorted(dataset_path.glob("year=*/month=*/*.parquet"))


def partition_key(file: Path) -> tuple[int, int]:
    """``(año, mes)`` de un archivo Parquet según su ruta ``year=YYYY/month=MM/``."""
    year = int(hive_value(file.parent.parent.name, "year") or 0)
    month = int(hive_value(file.parent.name, "month") or 0)
    return year, month


def find_dataset_path(base_dir: Path, identifier: DatasetIdentifier) -> Path | None:
    """Raíz Hive del dataset concreto, o ``None`` si no existe, no tiene Parquet o escapa de
    ``base_dir``."""
    path = (
        base_dir
        / identifier.exchange
        / f"market_type={identifier.market_type}"
        / f"symbol={identifier.symbol}"
        / f"timeframe={identifier.timeframe}"
    )
    inside = path.resolve().is_relative_to(base_dir.resolve())
    if not inside or not path.is_dir() or not dataset_files(path):
        return None
    return path


def dataset_label(identifier: DatasetIdentifier) -> str:
    """Identificador legible ``exchange/market_type/symbol/timeframe`` (campo ``dataset_id``)."""
    return "/".join(
        (identifier.exchange, identifier.market_type, identifier.symbol, identifier.timeframe)
    )
