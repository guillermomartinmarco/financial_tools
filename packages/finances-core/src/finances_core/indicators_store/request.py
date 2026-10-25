"""Gramática compartida ``name[:k=v,...]`` (``--indicator`` / ``--function``).

Gramática::

    token  := name (':' param (',' param)*)?
    param  := key '=' value
    name   := [A-Za-z_][A-Za-z0-9_]*          (conserva mayúsculas)
    key    := [A-Za-z_][A-Za-z0-9_]*          (normalizada a minúsculas)
    value  := entero | decimal | identificador (normalizado a minúsculas)

Solo el nombre distingue mayúsculas: cada herramienta decide cómo normalizarlo
(``ft_indicators`` lo valida en minúsculas; ``ft_ta_lib`` lo pasa a MAYÚSCULAS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ParamValue = int | float | str

GRAMMAR_HINT = "name[:k=v,...]"

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INT_RE = re.compile(r"[-+]?\d+")
_FLOAT_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


class SpecTokenError(ValueError):
    """Token ``name[:k=v,...]`` con sintaxis inválida."""

    def __init__(self, token: str, reason: str) -> None:
        super().__init__(f"Especificación inválida {token!r}: {reason} (formato {GRAMMAR_HINT})")
        self.token = token
        self.reason = reason


@dataclass(frozen=True)
class ParsedSpec:
    """Resultado del parseo: nombre tal cual y parámetros tipados."""

    name: str
    params: dict[str, ParamValue] = field(default_factory=dict)


def _parse_value(token: str, key: str, raw: str) -> ParamValue:
    if _INT_RE.fullmatch(raw):
        return int(raw)
    if _FLOAT_RE.fullmatch(raw):
        return float(raw)
    if _IDENT_RE.fullmatch(raw):
        return raw.lower()
    raise SpecTokenError(token, f"valor {raw!r} de {key!r} no es entero, decimal ni identificador")


def parse_spec_token(token: str) -> ParsedSpec:
    """Parsea ``name[:k=v,...]`` a :class:`ParsedSpec`.

    Raises:
        SpecTokenError: SI el nombre falta o no es un identificador, un parámetro no
            tiene la forma ``clave=valor``, la clave o el valor son inválidos, o una
            clave se repite.
    """
    name_part, sep, params_part = token.strip().partition(":")
    name = name_part.strip()
    if not _IDENT_RE.fullmatch(name):
        reason = f"nombre {name!r} no es un identificador [A-Za-z_][A-Za-z0-9_]*"
        raise SpecTokenError(token, reason)
    if not sep:
        return ParsedSpec(name=name)

    params: dict[str, ParamValue] = {}
    for raw_param in params_part.split(","):
        param = raw_param.strip()
        if not param:
            raise SpecTokenError(token, "parámetro vacío")
        key_raw, eq, value_raw = param.partition("=")
        if not eq:
            raise SpecTokenError(token, f"parámetro {param!r} no tiene la forma clave=valor")
        key, value = key_raw.strip().lower(), value_raw.strip()
        if not _IDENT_RE.fullmatch(key):
            raise SpecTokenError(token, f"clave {key_raw.strip()!r} no es un identificador")
        if not value:
            raise SpecTokenError(token, f"valor vacío para {key!r}")
        if key in params:
            raise SpecTokenError(token, f"parámetro {key!r} repetido")
        params[key] = _parse_value(token, key, value)
    return ParsedSpec(name=name, params=params)
