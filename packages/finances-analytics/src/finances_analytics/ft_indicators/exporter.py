"""Traducción de ``--indicator`` a ``series_id`` para exportar (RF-9; plan §3.7).

La vista ancha y los formatos viven en el núcleo (``finances_core.indicators_store.view``
y ``finances_core.export``); este módulo solo resuelve qué series pide el usuario:

- un ``indicator_id`` ya calculado (``rsi_14``, ``macd_12_26_9``) se usa tal cual;
- una spec ``name[:k=v,...]`` (``rsi``, ``bb:std=2.5``) se resuelve con el catálogo a su
  ``indicator_id`` (``rsi_14``, ``bb_20_2p5``).
"""

from __future__ import annotations

from collections.abc import Sequence

from finances_core.export import ExportFormat
from finances_core.indicators_store.namespaces import NamespaceViolationError, validate_series_id
from finances_core.schemas.data_viewer import DatasetIdentifier

from .catalog import CATALOG, NAMESPACE, resolve
from .errors import IndicatorValidationError
from .request import spec_from_token


def series_id_for(token: str) -> str:
    """``indicator_id`` de un token ``indicator_id`` o ``name[:k=v,...]``.

    Raises:
        IndicatorValidationError: SI la spec es inválida o el id no pertenece al espacio
            de nombres ``ft_indicators`` (p. ej. ``talib_rsi_14``).
    """
    text = token.strip()
    if ":" in text or text.lower() in CATALOG:
        return resolve(spec_from_token(text)).indicator_id
    try:
        return validate_series_id(NAMESPACE, text.lower())
    except NamespaceViolationError as exc:
        msg = f"indicador {token!r} inválido: {exc}"
        raise IndicatorValidationError(msg, indicator=token) from exc


def series_ids(tokens: Sequence[str]) -> list[str]:
    """Tokens traducidos a ``indicator_id`` sin repetir (conserva el orden)."""
    return list(dict.fromkeys(series_id_for(t) for t in tokens))


def export_file_name(ds: DatasetIdentifier, fmt: ExportFormat) -> str:
    """``{exchange}_{market_type}_{symbol}_{timeframe}_indicators.{ext}`` (RF-9)."""
    return f"{ds.exchange}_{ds.market_type}_{ds.symbol}_{ds.timeframe}_indicators.{fmt}"


__all__ = ["export_file_name", "series_id_for", "series_ids"]
