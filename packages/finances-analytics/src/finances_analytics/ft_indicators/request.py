"""Adaptadores de la petición canónica ``IndicatorComputeRequest`` (RF-5, plan §2.1).

- ``--indicator name[:k=v,...]`` → :func:`spec_from_token` (gramática compartida
  ``finances_core.indicators_store.request.parse_spec_token``).
- ``--request-file FILE.json`` → :func:`load_request_file` (exactamente el cuerpo de la API).
- :func:`dedupe_specs`: specs repetidas (mismo ``indicator_id``) se deduplican sin error.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from finances_core.indicators_store.request import SpecTokenError, parse_spec_token
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import IndicatorComputeRequest, IndicatorSpec
from pydantic import ValidationError

from .catalog import CATALOG, resolve
from .errors import IndicatorValidationError


def spec_from_token(token: str) -> IndicatorSpec:
    """``name[:k=v,...]`` → :class:`IndicatorSpec` (nombre normalizado a minúsculas).

    Raises:
        IndicatorValidationError: SI la sintaxis es inválida o el indicador no existe
            (el mensaje lista los nombres válidos).
    """
    try:
        parsed = parse_spec_token(token)
    except SpecTokenError as exc:
        raise IndicatorValidationError(str(exc)) from exc
    name = parsed.name.lower()
    if name not in CATALOG:
        msg = f"Indicador desconocido {parsed.name!r}; válidos: {', '.join(CATALOG)}"
        raise IndicatorValidationError(msg, indicator=parsed.name, valid=", ".join(CATALOG))
    return IndicatorSpec.model_validate({"name": name, "params": parsed.params})


def build_request(datasets: Sequence[str], indicators: Sequence[str]) -> IndicatorComputeRequest:
    """Petición canónica desde ``--dataset ex/mt/sym/tf`` y ``--indicator`` repetidos.

    Raises:
        IndicatorValidationError: SI algún dataset o indicador es inválido o se exceden
            los límites (1–100 datasets, 1–50 indicadores).
    """
    try:
        parsed_datasets = [DatasetIdentifier.parse(d) for d in datasets]
    except ValueError as exc:
        msg = f"dataset inválido: {exc}"
        raise IndicatorValidationError(msg) from exc
    specs = [spec_from_token(t) for t in indicators]
    try:
        return IndicatorComputeRequest(datasets=parsed_datasets, indicators=specs)
    except ValidationError as exc:
        msg = f"petición inválida: {exc}"
        raise IndicatorValidationError(msg) from exc


def load_request_file(path: Path) -> IndicatorComputeRequest:
    """Carga ``--request-file`` (mismo JSON que el cuerpo de ``POST /indicators/compute``).

    Raises:
        IndicatorValidationError: SI el archivo no existe, no es JSON válido o no cumple
            el contrato.
    """
    if not path.is_file():
        msg = f"El archivo de petición no existe: {path}"
        raise IndicatorValidationError(msg)
    try:
        return IndicatorComputeRequest.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        msg = f"archivo de petición inválido {path}: {exc}"
        raise IndicatorValidationError(msg) from exc


def dedupe_specs(specs: Sequence[IndicatorSpec]) -> list[IndicatorSpec]:
    """Specs con defaults resueltos, sin repetir ``indicator_id`` (conserva el primer orden).

    Raises:
        IndicatorValidationError: SI alguna spec es inválida (validación sin dataset).
    """
    unique: dict[str, IndicatorSpec] = {}
    for spec in specs:
        resolved = resolve(spec)
        unique.setdefault(resolved.indicator_id, resolved.spec())
    return list(unique.values())


__all__ = ["build_request", "dedupe_specs", "load_request_file", "spec_from_token"]
