"""Errores tipados de ft_data_viewer (Art. 6).

Cada clase se mapea 1:1 a un requisito EARS y a un código de salida CLI / estado HTTP:

| Error                   | Requisito         | CLI exit | HTTP |
|-------------------------|-------------------|----------|------|
| DatasetNotFoundError    | RF-2              | 1        | 404  |
| InvalidArgumentError    | T5.4 / DV-01      | 2        | 400  |
| NoDataInRangeError      | RF-6              | 3        | 400  |
| OSError (IO)            | T5.4              | 4        | 500  |
"""

from __future__ import annotations


class DataViewerError(Exception):
    """Base de los errores de negocio de ft_data_viewer."""


class DatasetNotFoundError(DataViewerError):
    """SI el dataset solicitado no existe, ENTONCES 404 / CLI exit 1 (RF-2)."""


class InvalidArgumentError(DataViewerError, ValueError):
    """Argumentos inválidos (market_type, timeframe, columnas, rango, destino)."""


class NoDataInRangeError(DataViewerError):
    """SI el rango solicitado no tiene datos, ENTONCES 400 / CLI exit 3 (RF-6)."""
