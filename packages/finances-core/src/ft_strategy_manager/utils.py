"""Utilidades puras: JSON canónico, hash, validación, JSON Patch, diff y enmascarado."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import jsonpatch
import jsonpointer
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from finances_core.schemas.strategy import DiffResult
from ft_strategy_manager.errors import StrategyValidationError

#: Claves cuyo valor se enmascara en logs de auditoría (Art. 9).
_SECRET_KEY = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private[_-]?key)", re.IGNORECASE
)
MASK = "***"


def canonical_json(obj: Any) -> str:
    """JSON determinístico para hashing: claves ordenadas, sin espacios en blanco."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def compute_hash(parameters: dict[str, Any]) -> str:
    """SHA-256 hexadecimal del JSON canónico de ``parameters``."""
    return hashlib.sha256(canonical_json(parameters).encode("utf-8")).hexdigest()


def check_parameter_schema(schema: dict[str, Any]) -> None:
    """RF-7: verifica que ``schema`` sea un JSON Schema draft-07 válido.

    Raises:
        StrategyValidationError: SI el schema es inválido.
    """
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        msg = f"Invalid parameter_schema: {exc.message}"
        raise StrategyValidationError(msg) from exc


def validate_parameters(
    parameters: dict[str, Any], schema: dict[str, Any] | None
) -> tuple[bool, str | None]:
    """RF-7: valida ``parameters`` contra ``schema`` (modo permisivo si es ``None``).

    Returns:
        ``(True, None)`` o ``(False, errores)``, con todos los errores y su ruta.
    """
    if schema is None:
        return True, None
    errors = sorted(Draft7Validator(schema).iter_errors(parameters), key=lambda e: list(e.path))
    if not errors:
        return True, None
    details = "; ".join(
        f"{'$' + ''.join(f'[{p!r}]' for p in e.absolute_path)}: {e.message}" for e in errors
    )
    return False, details


def apply_json_patch(parameters: dict[str, Any], patch: list[dict[str, Any]]) -> dict[str, Any]:
    """RF-3: aplica un JSON Patch RFC 6902 sin mutar ``parameters``.

    Raises:
        StrategyValidationError: SI el patch es inválido o no aplica, o el resultado
            deja de ser un objeto.
    """
    try:
        result = jsonpatch.JsonPatch(patch).apply(parameters, in_place=False)
    except (
        jsonpatch.JsonPatchException,
        jsonpointer.JsonPointerException,
    ) as exc:
        msg = f"Invalid parameters_patch: {exc}"
        raise StrategyValidationError(msg) from exc
    if not isinstance(result, dict):
        msg = "Invalid parameters_patch: the result must be a JSON object"
        raise StrategyValidationError(msg)
    return result


def compute_diff(
    from_params: dict[str, Any],
    to_params: dict[str, Any],
    from_version: int = 0,
    to_version: int = 0,
) -> DiffResult:
    """RF-6: diff semántico de primer nivel (added/removed/changed).

    La comparación distingue tipos JSON (``1`` != ``1.0`` != ``true``) comparando la forma
    canónica de cada valor.
    """
    added = {k: v for k, v in to_params.items() if k not in from_params}
    removed = {k: v for k, v in from_params.items() if k not in to_params}
    changed = {
        k: (from_params[k], to_params[k])
        for k in from_params
        if k in to_params and canonical_json(from_params[k]) != canonical_json(to_params[k])
    }
    return DiffResult(
        from_version=from_version,
        to_version=to_version,
        added=added,
        removed=removed,
        changed=changed,
    )


def mask_secrets(value: Any) -> Any:
    """Copia de ``value`` con los valores de claves sensibles reemplazados por ``***``."""
    if isinstance(value, dict):
        return {
            k: (MASK if _SECRET_KEY.search(str(k)) else mask_secrets(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [mask_secrets(item) for item in value]
    return value
