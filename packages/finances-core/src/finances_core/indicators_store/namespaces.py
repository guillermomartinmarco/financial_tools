"""Espacios de nombres por herramienta (ADR-I14 / ADR-T03).

``ft_indicators`` y ``ft_ta_lib`` comparten ``FT_INDICATORS_DIR`` y los mismos
directorios mensuales; no colisionan porque:

- ``ft_indicators``: ``series_id`` sin prefijo y **nunca** empezando por ``talib_``.
- ``ft_ta_lib``: ``series_id`` que **siempre** empieza por ``talib_``.
- Cada una tiene su manifiesto ``_manifest.{namespace}.json`` y su bloqueo
  ``_lock.{namespace}`` (el prefijo ``_`` hace que los lectores Hive los ignoren).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from finances_core.schemas.indicators import Namespace

_SAFE_ID_RE = re.compile(r"^[a-z0-9_]+$")
TALIB_PREFIX = "talib_"


class NamespaceViolationError(ValueError):
    """``series_id`` inseguro o perteneciente al espacio de nombres de otra herramienta."""


@dataclass(frozen=True)
class NamespaceRules:
    """Reglas de un espacio de nombres."""

    name: Namespace
    prefix: str  # prefijo obligatorio ("" = ninguno)
    forbidden_prefixes: tuple[str, ...]  # prefijos reservados a otras herramientas


NAMESPACES: dict[str, NamespaceRules] = {
    "ft_indicators": NamespaceRules("ft_indicators", "", (TALIB_PREFIX,)),
    "ft_ta_lib": NamespaceRules("ft_ta_lib", TALIB_PREFIX, ()),
}


def get_rules(namespace: Namespace) -> NamespaceRules:
    """Reglas de ``namespace``.

    Raises:
        NamespaceViolationError: SI el espacio de nombres no existe.
    """
    try:
        return NAMESPACES[namespace]
    except KeyError:
        msg = f"Espacio de nombres desconocido {namespace!r}; válidos: {sorted(NAMESPACES)}"
        raise NamespaceViolationError(msg) from None


def _violation(namespace: Namespace, series_id: str) -> str | None:
    rules = get_rules(namespace)
    if not _SAFE_ID_RE.fullmatch(series_id):
        return "no es un identificador [a-z0-9_]"
    if rules.prefix and (
        not series_id.startswith(rules.prefix) or len(series_id) == len(rules.prefix)
    ):
        return f"debe empezar por {rules.prefix!r} seguido del nombre"
    for reserved in rules.forbidden_prefixes:
        if series_id.startswith(reserved):
            return f"usa el prefijo reservado {reserved!r}"
    return None


def validate_series_id(namespace: Namespace, series_id: str) -> str:
    """Devuelve ``series_id`` si pertenece a ``namespace``.

    Raises:
        NamespaceViolationError: SI es inseguro o pertenece a otra herramienta.
    """
    reason = _violation(namespace, series_id)
    if reason is not None:
        msg = f"series_id {series_id!r} inválido en {namespace}: {reason}"
        raise NamespaceViolationError(msg)
    return series_id


def belongs_to(namespace: Namespace, series_id: str) -> bool:
    """``True`` si ``series_id`` es válido en ``namespace``."""
    return _violation(namespace, series_id) is None


def manifest_file_name(namespace: Namespace) -> str:
    """``_manifest.{namespace}.json``."""
    return f"_manifest.{get_rules(namespace).name}.json"


def lock_file_name(namespace: Namespace) -> str:
    """``_lock.{namespace}``."""
    return f"_lock.{get_rules(namespace).name}"
