"""Errores tipados de :mod:`finances_core.datasets` (RF-12).

Cada error lleva un ``code`` estable; los adaptadores (CLI/API de ``ft_datasets``) los
traducen a códigos de salida y estados HTTP sin inspeccionar mensajes.
"""

from __future__ import annotations


class DatasetsError(Exception):
    """Base de los errores de recetas de datasets."""

    code = "datasets_error"


class RecipeNotFoundError(DatasetsError, LookupError):
    """La receta no existe en ``FT_DATASETS_RECIPES_DIR`` (exit 1 / HTTP 404)."""

    code = "recipe_not_found"


class ItemNotFoundError(DatasetsError, LookupError):
    """La clave de ítem no pertenece a la receta."""

    code = "item_not_found"


class InvalidRecipeError(DatasetsError, ValueError):
    """Receta inválida, nombre inválido o rango no reproducible (exit 2 / HTTP 400).

    ``detail`` conserva el detalle completo (p. ej. el de Pydantic).
    """

    code = "invalid_recipe"

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail or message


class InvalidLockError(DatasetsError, ValueError):
    """*Lock* corrupto, de otra receta o de otro algoritmo (exit 2 / HTTP 400).

    Nunca se reinterpreta un *lock* inválido (RF-12).
    """

    code = "invalid_lock"


class DatasetDataError(DatasetsError, ValueError):
    """Datos no canónicos en el almacén: dtype, columnas, orden u ``open_time_ms`` duplicado."""

    code = "invalid_data"
