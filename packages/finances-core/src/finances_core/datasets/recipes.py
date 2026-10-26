"""Recetas ``datasets/<nombre>.toml`` y *locks* ``datasets/<nombre>.lock.json`` (plan §3.1).

La receta nunca se reescribe (``tomllib`` es de solo lectura); el *lock* es JSON estable
(``indent=2``, claves ordenadas, salto de línea final) y se escribe de forma atómica
(temporal en el mismo directorio + ``replace``).
"""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from datetime import date, timedelta
from pathlib import Path

import structlog
from pydantic import ValidationError

from finances_core.datasets.errors import (
    InvalidLockError,
    InvalidRecipeError,
    ItemNotFoundError,
    RecipeNotFoundError,
)
from finances_core.schemas.datasets import (
    HASH_ALGORITHM,
    RECIPE_NAME_PATTERN,
    DatasetRecipe,
    RecipeItem,
    RecipeLock,
)

logger = structlog.get_logger(__name__)

RECIPE_SUFFIX = ".toml"
LOCK_SUFFIX = ".lock.json"
_NAME_RE = re.compile(RECIPE_NAME_PATTERN)


def _checked_path(recipes_dir: Path, name: str, suffix: str) -> Path:
    """Ruta de ``name + suffix`` **dentro** de ``recipes_dir`` (sin rutas arbitrarias)."""
    if _NAME_RE.fullmatch(name) is None:
        msg = f"nombre de receta inválido {name!r} (se espera {RECIPE_NAME_PATTERN})"
        raise InvalidRecipeError(msg)
    base = recipes_dir.resolve()
    path = (base / f"{name}{suffix}").resolve()
    if not path.is_relative_to(base):
        msg = f"nombre de receta inválido {name!r}: sale de {base}"
        raise InvalidRecipeError(msg)
    return path


def recipe_path(recipes_dir: Path, name: str) -> Path:
    """Ruta del TOML de la receta ``name`` (validada)."""
    return _checked_path(recipes_dir, name, RECIPE_SUFFIX)


def lock_path(recipes_dir: Path, name: str) -> Path:
    """Ruta del *lock* de la receta ``name`` (validada)."""
    return _checked_path(recipes_dir, name, LOCK_SUFFIX)


def list_recipes(recipes_dir: Path) -> list[str]:
    """Nombres de receta válidos en ``recipes_dir`` (orden alfabético; sin leerlas)."""
    if not recipes_dir.is_dir():
        return []
    names = (p.name[: -len(RECIPE_SUFFIX)] for p in recipes_dir.glob(f"*{RECIPE_SUFFIX}"))
    return sorted(n for n in names if _NAME_RE.fullmatch(n))


def load_recipe(recipes_dir: Path, name: str) -> DatasetRecipe:
    """Lee y valida la receta ``name`` (RF-1).

    Raises:
        InvalidRecipeError: nombre inválido, TOML inválido, contrato incumplido o ``name``
            del TOML distinto del nombre del archivo.
        RecipeNotFoundError: SI el archivo no existe.
        OSError: error de IO al leerla.
    """
    path = recipe_path(recipes_dir, name)
    if not path.is_file():
        msg = f"Receta no encontrada: {name} (en {path.parent})"
        raise RecipeNotFoundError(msg)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"TOML inválido en {path.name}: {exc}"
        raise InvalidRecipeError(msg) from exc
    try:
        recipe = DatasetRecipe.model_validate(raw)
    except ValidationError as exc:
        msg = f"Receta {name} inválida: {exc}"
        raise InvalidRecipeError(msg, detail=str(exc)) from exc
    if recipe.name != name:
        msg = f"La receta {path.name} declara name={recipe.name!r}; debe ser {name!r}"
        raise InvalidRecipeError(msg)
    return recipe


def validate_reproducible(recipe: DatasetRecipe, today: date, settle_days: int) -> None:
    """RF-2: todo ``end`` debe ser ``<= today - settle_days`` (rango cerrado y asentado).

    Raises:
        InvalidRecipeError: SI algún bloque termina dentro del margen de asentamiento.
    """
    limit = today - timedelta(days=settle_days)
    recent = sorted({block.end.isoformat() for block in recipe.datasets if block.end > limit})
    if recent:
        msg = (
            f"Receta {recipe.name} no reproducible: end {recent} posterior a {limit.isoformat()} "
            f"(hoy {today.isoformat()} - FT_DATASETS_SETTLE_DAYS={settle_days})"
        )
        raise InvalidRecipeError(msg)


def canonical_sha256(recipe: DatasetRecipe) -> str:
    """Hash canónico de la receta (plan §3.3); ver :meth:`DatasetRecipe.canonical_sha256`."""
    return recipe.canonical_sha256()


def find_item(recipe: DatasetRecipe, key: str) -> RecipeItem:
    """Ítem de ``recipe`` con clave ``key``.

    Raises:
        ItemNotFoundError: SI la clave no pertenece a la receta.
    """
    for item in recipe.items():
        if item.key == key:
            return item
    msg = f"Ítem {key!r} no pertenece a la receta {recipe.name}"
    raise ItemNotFoundError(msg)


def load_lock(recipes_dir: Path, name: str) -> RecipeLock | None:
    """*Lock* de la receta ``name`` o ``None`` si no existe.

    Raises:
        InvalidLockError: JSON corrupto, otro algoritmo/versión, contrato incumplido o
            *lock* de otra receta (nunca se reinterpreta).
        OSError: error de IO al leerlo.
    """
    path = lock_path(recipes_dir, name)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"Lock corrupto {path.name}: JSON inválido ({exc})"
        raise InvalidLockError(msg) from exc
    algorithm = raw.get("hash_algorithm") if isinstance(raw, dict) else None
    if algorithm != HASH_ALGORITHM:
        msg = f"Lock {path.name} usa hash_algorithm={algorithm!r}; se espera {HASH_ALGORITHM!r}"
        raise InvalidLockError(msg)
    try:
        lock = RecipeLock.model_validate(raw)
    except ValidationError as exc:
        msg = f"Lock {path.name} inválido: {exc}"
        raise InvalidLockError(msg) from exc
    if lock.recipe != name:
        msg = f"Lock {path.name} pertenece a la receta {lock.recipe!r}, no a {name!r}"
        raise InvalidLockError(msg)
    return lock


def lock_json(lock: RecipeLock) -> str:
    """Serialización estable del *lock* (``indent=2``, claves ordenadas, salto final)."""
    return json.dumps(lock.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def write_lock(recipes_dir: Path, lock: RecipeLock) -> Path:
    """Escribe el *lock* de forma atómica; ante cualquier fallo conserva el previo.

    Raises:
        OSError: error de IO (el temporal se elimina y el *lock* previo queda intacto).
    """
    path = lock_path(recipes_dir, lock.recipe)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = lock_json(lock).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        tmp = Path(handle.name)
    try:
        tmp.write_bytes(payload)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info("dataset_lock_written", recipe=lock.recipe, path=str(path), items=len(lock.items))
    return path
