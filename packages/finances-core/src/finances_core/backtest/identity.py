"""Identidad de una corrida de backtest (``data_hash``, ``run_id``; RF-8, plan §3.3, A10).

Funciones puras y deterministas entre sistemas operativos:

- :func:`data_hash` hashea los bytes little-endian de ``open_time_ms`` (``Int64``) y de
  ``open``/``high``/``low``/``close`` (``Float32``), columna a columna y en ese orden. No
  depende de los bytes del Parquet, del troceado de Polars ni de la representación
  textual de los flotantes.
- :func:`run_id` es el SHA-256 del JSON canónico (claves ordenadas, sin espacios) de
  :func:`run_identity`. ``force`` y ``created_at_ms`` no forman parte de la identidad.
"""

from __future__ import annotations

import hashlib
import json
import sys
from array import array
from typing import Any

import polars as pl

from finances_core.backtest.catalog import CATALOG_VERSION, RESULT_SCHEMA_VERSION
from finances_core.schemas.backtest import BacktestRequest, ResolvedStrategy

#: Columnas de velas que entran en ``data_hash``, en orden, con su tipo y código ``array``.
_HASHED_COLUMNS: tuple[tuple[str, pl.DataType, str], ...] = (
    ("open_time_ms", pl.Int64(), "q"),
    ("open", pl.Float32(), "f"),
    ("high", pl.Float32(), "f"),
    ("low", pl.Float32(), "f"),
    ("close", pl.Float32(), "f"),
)


def data_hash(candles: pl.DataFrame) -> str:
    """SHA-256 (hex) del contenido canónico de las velas del rango cargado (RF-8).

    Las demás columnas (p. ej. ``volume``) y el orden de las columnas no influyen.

    Raises:
        ValueError: SI falta una columna hasheada o contiene nulos.
        TypeError: SI una columna hasheada no tiene su tipo canónico.
    """
    digest = hashlib.sha256()
    for name, dtype, code in _HASHED_COLUMNS:
        if name not in candles.columns:
            msg = f"data_hash: falta la columna {name!r}"
            raise ValueError(msg)
        series = candles.get_column(name)
        if series.dtype != dtype:
            msg = f"data_hash: la columna {name!r} es {series.dtype}, se esperaba {dtype}"
            raise TypeError(msg)
        if series.null_count():
            msg = f"data_hash: la columna {name!r} contiene nulos"
            raise ValueError(msg)
        values = array(code, series.to_list())
        if sys.byteorder != "little":
            values.byteswap()
        digest.update(values.tobytes())
    return digest.hexdigest()


def run_identity(  # noqa: PLR0913 - campos de identidad exigidos por RF-8
    request: BacktestRequest,
    strategy: ResolvedStrategy,
    *,
    start_ms: int,
    end_ms: int,
    data_hash: str,
    engine: str,
    engine_version: str,
) -> dict[str, Any]:
    """Diccionario de RF-8 del que se deriva el ``run_id``.

    ``start_ms``/``end_ms`` son los **efectivos** (primera y última vela cargadas), no
    los pedidos. ``strategy.hash`` (de ``ft_strategy_manager``) ya cubre ``kind`` y
    ``params``; ``force`` y ``created_at_ms`` no forman parte de la identidad.
    """
    return {
        "strategy_hash": strategy.hash,
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.version,
        "kind_version": strategy.kind_version,
        "strategy_code_hash": strategy.code_hash,
        "dataset": request.dataset.model_dump(mode="json"),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "data_hash": data_hash,
        "initial_cash": request.initial_cash,
        "size_fraction": request.size_fraction,
        "fees_bps": request.fees_bps,
        "slippage_bps": request.slippage_bps,
        "engine": engine,
        "engine_version": engine_version,
        "catalog_version": CATALOG_VERSION,
        "result_schema_version": RESULT_SCHEMA_VERSION,
    }


def run_id(  # noqa: PLR0913 - campos de identidad exigidos por RF-8
    request: BacktestRequest,
    strategy: ResolvedStrategy,
    *,
    start_ms: int,
    end_ms: int,
    data_hash: str,
    engine: str,
    engine_version: str,
) -> str:
    """``run_id`` (SHA-256 hex del JSON canónico de :func:`run_identity`; RF-8)."""
    payload = run_identity(
        request,
        strategy,
        start_ms=start_ms,
        end_ms=end_ms,
        data_hash=data_hash,
        engine=engine,
        engine_version=engine_version,
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
