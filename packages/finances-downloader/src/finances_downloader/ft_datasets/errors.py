"""Traducción de errores de dominio a códigos de salida y estados HTTP (RF-9, RF-12).

| Error | Exit | HTTP |
|---|---|---|
| ``RecipeNotFoundError`` | 1 | 404 |
| ``InvalidRecipeError`` / ``InvalidLockError`` / ``ValueError`` | 2 | 400 |
| ``ConfigError`` (``FT_DATASETS_DIR`` anidado con ``FT_PARQUET_DIR``) | 2 | 500 |
| ``DatasetDataError`` (datos del almacén no canónicos: integridad, como un *mismatch*) | 5 | 409 |
| ``OSError`` | 4 | 500 |
"""

from __future__ import annotations

from finances_core.config import ConfigError
from finances_core.datasets.errors import (
    DatasetDataError,
    DatasetsError,
    InvalidLockError,
    InvalidRecipeError,
    RecipeNotFoundError,
)

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VALIDATION = 2
EXIT_FAILED = 3
EXIT_IO = 4
EXIT_VERIFY_FAILED = 5
EXIT_UNLOCKED = 6

HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_SERVER_ERROR = 500

#: Errores de dominio que los adaptadores traducen (el resto se propaga).
DOMAIN_ERRORS: tuple[type[Exception], ...] = (DatasetsError, ConfigError, OSError, ValueError)

#: Código de salida de ``verify`` / ``materialize`` por estado global u *outcome*.
OUTCOME_EXIT: dict[str, int] = {
    "OK": EXIT_OK,
    "NOT_VERIFIED": EXIT_OK,
    "MISMATCH": EXIT_VERIFY_FAILED,
    "RECIPE_CHANGED": EXIT_VERIFY_FAILED,
    "UNLOCKED": EXIT_UNLOCKED,
    "DOWNLOAD_FAILED": EXIT_FAILED,
}


def exit_code_for(exc: Exception) -> int:
    """Código de salida de la CLI para un error de dominio."""
    if isinstance(exc, RecipeNotFoundError):
        return EXIT_GENERIC
    if isinstance(exc, DatasetDataError):
        return EXIT_VERIFY_FAILED
    if isinstance(exc, InvalidRecipeError | InvalidLockError | ConfigError | ValueError):
        return EXIT_VALIDATION
    if isinstance(exc, OSError):
        return EXIT_IO
    return EXIT_GENERIC


def http_status_for(exc: Exception) -> int:
    """Estado HTTP de la API para un error de dominio."""
    if isinstance(exc, RecipeNotFoundError):
        return HTTP_NOT_FOUND
    if isinstance(exc, DatasetDataError):
        return HTTP_CONFLICT
    if isinstance(exc, InvalidRecipeError | InvalidLockError):
        return HTTP_BAD_REQUEST
    if isinstance(exc, ConfigError | OSError):
        return HTTP_SERVER_ERROR
    if isinstance(exc, ValueError):
        return HTTP_BAD_REQUEST
    return HTTP_SERVER_ERROR
