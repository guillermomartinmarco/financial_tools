"""Errores tipados de ft_market_data_manager (Art. 6, RF-10).

Cada clase se mapea a un código de salida CLI / estado HTTP (la traducción vive en
``cli.py`` y ``api.py``):

| Error                      | CLI exit | HTTP      |
|----------------------------|----------|-----------|
| DatasetNotFoundError       | 1        | 404       |
| InvalidArgumentError       | 2        | 400 / 422 |
| ImportValidationError      | 2        | 422       |
| ConflictError              | 3        | 409       |
| ConfirmationRequiredError  | 3        | 400       |
| OSError (IO)               | 4        | 500       |

El rechazo interactivo de la confirmación (exit 1) lo resuelve la CLI.
"""

from __future__ import annotations


class MarketDataManagerError(Exception):
    """Base de los errores de negocio de ft_market_data_manager."""


class DatasetNotFoundError(MarketDataManagerError):
    """SI el dataset destino o el archivo de origen no existe, ENTONCES 404 / exit 1."""


class InvalidArgumentError(MarketDataManagerError, ValueError):
    """Argumentos inválidos (selector, criterio de ``prune``, destino)."""


class ImportValidationError(InvalidArgumentError):
    """Columnas, timestamp, alineación o filas inválidas en ``import`` (422 / exit 2)."""


class ConflictError(MarketDataManagerError):
    """Conflicto con ``--on-conflict fail`` o divergencia > 1 % sin ``--force`` (409 / exit 3)."""


class ConfirmationRequiredError(MarketDataManagerError):
    """SI se pide borrar o modificar sin ``confirm``/``--force`` ni ``dry_run``, ENTONCES
    400 / exit 3 y no se toca nada (RF-3)."""


class PartitionReadError(MarketDataManagerError):
    """Una partición no se puede leer o no cumple la forma canónica; se informa como
    ``error`` en su ``PartitionResult`` sin abortar las demás (RF-6)."""
