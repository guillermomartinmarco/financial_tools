"""Traducción de ``--function`` a ``series_id`` para exportar (RF-10; plan §3.4).

La vista ancha y los formatos viven en el núcleo (``finances_core.indicators_store.view``
y ``finances_core.export``); este módulo solo resuelve qué series pide el usuario:

- un ``series_id`` ya calculado (``talib_rsi_14``, ``talib_bbands_5_2_2_sma``) se usa tal cual;
- una spec ``NAME[:k=v,...]`` (``rsi``, ``bbands:matype=ema``) se resuelve con el catálogo de
  TA-Lib a su ``series_id`` (``talib_rsi_14``, ``talib_bbands_5_2_2_ema``).

Solo se aceptan series del espacio de nombres ``ft_ta_lib`` (prefijo ``talib_``).
"""

from __future__ import annotations

from collections.abc import Sequence

from finances_core.export import ExportFormat
from finances_core.indicators_store.namespaces import NamespaceViolationError, validate_series_id
from finances_core.schemas.data_viewer import DatasetIdentifier

from .catalog import function_table
from .errors import TaLibValidationError
from .request import spec_from_token
from .resolver import NAMESPACE, resolve

__all__ = ["export_file_name", "series_id_for", "series_ids"]


def series_id_for(token: str) -> str:
    """``series_id`` de un token ``series_id`` o ``NAME[:k=v,...]``.

    Raises:
        TaLibValidationError: SI la spec es inválida o el id no pertenece al espacio de
            nombres ``ft_ta_lib`` (p. ej. ``rsi_14`` de ``ft_indicators``).
    """
    text = token.strip()
    if ":" in text or text.upper() in function_table():
        return resolve(spec_from_token(text)).series_id
    try:
        return validate_series_id(NAMESPACE, text.lower())
    except NamespaceViolationError as exc:
        msg = f"función {token!r} inválida: {exc}"
        raise TaLibValidationError(msg, function=token) from exc


def series_ids(tokens: Sequence[str]) -> list[str]:
    """Tokens traducidos a ``series_id`` sin repetir (conserva el orden)."""
    return list(dict.fromkeys(series_id_for(t) for t in tokens))


def export_file_name(ds: DatasetIdentifier, fmt: ExportFormat) -> str:
    """``{exchange}_{market_type}_{symbol}_{timeframe}_ta_lib.{ext}`` (RF-10).

    El sufijo ``_ta_lib`` evita colisionar con la exportación de ``ft_indicators``
    (``…_indicators.{ext}``) en un mismo directorio de salida.
    """
    return f"{ds.exchange}_{ds.market_type}_{ds.symbol}_{ds.timeframe}_ta_lib.{fmt}"
