"""Adaptadores de la petición canónica ``TaLibComputeRequest`` (RF-5, plan §2.1).

- ``--function NAME[:k=v,...]`` → :func:`spec_from_token` (gramática compartida
  ``finances_core.indicators_store.request.parse_spec_token``): las claves que coinciden con
  un nombre de entrada de la función (``price``, ``price0``, ``price1``, ``prices``…) van a
  ``TaLibSpec.inputs``; el resto a ``params``.
- ``--request-file FILE.json`` → :func:`load_request_file` (exactamente el cuerpo de la API).
- :func:`expand`: grupos → sus funciones **soportadas** con defaults, resolución + ejecución
  de prueba de cada función, deduplicación por ``series_id`` (sin error) y límite de
  :data:`MAX_FUNCTIONS` tras expandir.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from finances_core.indicators_store.request import SpecTokenError, parse_spec_token
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.ta_lib import TaLibComputeRequest, TaLibSpec
from pydantic import ValidationError

from .catalog import function_table, groups
from .errors import TaLibValidationError
from .model import ResolvedFunction
from .resolver import lookup, probe, resolve

#: Funciones como máximo tras expandir grupos y deduplicar (RF-5).
MAX_FUNCTIONS = 200

__all__ = [
    "MAX_FUNCTIONS",
    "build_request",
    "expand",
    "group_functions",
    "load_request_file",
    "request_from_payload",
    "spec_from_token",
]


def spec_from_token(token: str) -> TaLibSpec:
    """``NAME[:k=v,...]`` → :class:`TaLibSpec` (nombre normalizado a MAYÚSCULAS).

    Raises:
        TaLibValidationError: SI la sintaxis es inválida, la función no existe (el mensaje
            lista los grupos válidos) o una columna de entrada no está permitida.
    """
    try:
        parsed = parse_spec_token(token)
    except SpecTokenError as exc:
        raise TaLibValidationError(str(exc)) from exc
    meta = lookup(parsed.name)
    input_names = {i.name for i in meta.inputs}
    data = {
        "function": meta.name,
        "params": {k: v for k, v in parsed.params.items() if k not in input_names},
        "inputs": {k: v for k, v in parsed.params.items() if k in input_names},
    }
    try:
        return TaLibSpec.model_validate(data)
    except ValidationError as exc:
        msg = f"especificación inválida {token!r}: {exc}"
        raise TaLibValidationError(msg, function=meta.name) from exc


def build_request(
    datasets: Sequence[str], functions: Sequence[str], groups_: Sequence[str]
) -> TaLibComputeRequest:
    """Petición canónica desde ``--dataset``, ``--function`` y ``--group`` repetidos.

    Raises:
        TaLibValidationError: SI algún dataset o función es inválido, no hay funciones ni
            grupos, o se exceden los límites de datasets (1–100).
    """
    try:
        parsed = [DatasetIdentifier.parse(d) for d in datasets]
    except ValueError as exc:
        msg = f"dataset inválido: {exc}"
        raise TaLibValidationError(msg) from exc
    specs = [spec_from_token(t) for t in functions]
    try:
        return TaLibComputeRequest(datasets=parsed, functions=specs, groups=list(groups_))
    except ValidationError as exc:
        msg = f"petición inválida: {exc}"
        raise TaLibValidationError(msg) from exc


def request_from_payload(data: object) -> TaLibComputeRequest:
    """Valida el cuerpo JSON ya decodificado de ``POST /ta-lib/compute`` (y ``--request-file``).

    Raises:
        TaLibValidationError: SI el cuerpo no cumple ``TaLibComputeRequest``.
    """
    try:
        return TaLibComputeRequest.model_validate(data)
    except ValidationError as exc:
        msg = f"petición inválida: {exc}"
        raise TaLibValidationError(msg) from exc


def load_request_file(path: Path) -> TaLibComputeRequest:
    """Carga ``--request-file`` (mismo JSON que el cuerpo de ``POST /ta-lib/compute``).

    Raises:
        TaLibValidationError: SI el archivo no existe, no es JSON válido o no cumple el
            contrato.
    """
    if not path.is_file():
        msg = f"El archivo de petición no existe: {path}"
        raise TaLibValidationError(msg)
    try:
        data = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"archivo de petición inválido {path}: {exc}"
        raise TaLibValidationError(msg) from exc
    return request_from_payload(data)


def group_functions(group: str) -> list[str]:
    """Funciones **soportadas** del grupo (nombre sin distinción de mayúsculas).

    Raises:
        TaLibValidationError: SI el grupo no existe (el mensaje lista los válidos).
    """
    available = groups()
    match = next((g for g in available if g.lower() == group.strip().lower()), None)
    if match is None:
        valid = ", ".join(available)
        msg = f"Grupo TA-Lib desconocido {group!r}; grupos válidos: {valid}"
        raise TaLibValidationError(msg, valid=valid)
    table = function_table()
    return [name for name in available[match] if table[name].supported]


def expand(req: TaLibComputeRequest) -> list[ResolvedFunction]:
    """Funciones de la petición resueltas, probadas y sin repetir ``series_id``.

    Orden: ``functions`` y después cada grupo; la primera aparición gana. La disponibilidad
    de columnas por dataset (``adj_close``) se valida al planificar.

    Raises:
        TaLibValidationError: SI alguna función o grupo es inválido, TA-Lib rechaza una
            combinación de parámetros, o hay más de :data:`MAX_FUNCTIONS` tras expandir.
    """
    specs = list(req.functions)
    for group in req.groups:
        specs.extend(TaLibSpec(function=name) for name in group_functions(group))
    unique: dict[str, ResolvedFunction] = {}
    for spec in specs:
        resolved = resolve(spec)
        if resolved.series_id not in unique:
            unique[resolved.series_id] = resolved
            if len(unique) > MAX_FUNCTIONS:
                msg = f"demasiadas funciones tras expandir grupos: más de {MAX_FUNCTIONS}"
                raise TaLibValidationError(msg, valid=f"1–{MAX_FUNCTIONS}")
    return [probe(fn) for fn in unique.values()]
