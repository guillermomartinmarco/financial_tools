"""Contratos Pydantic de ``ft_strategy_manager`` (specs/ft_strategy_manager/plan.md §1.1).

Los parámetros de una estrategia son JSON nativo (spec "Casos Límite"): ``int``, ``float``
finito, ``bool``, ``str``, ``None``, ``list`` y ``dict`` con claves ``str``, anidados sin
límite. La normalización ``float32``/``int64`` aplica en ejecución, no en definición.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

NAME_PATTERN = r"^[a-z][a-z0-9_-]{2,63}$"
HASH_PATTERN = r"^[a-f0-9]{64}$"
DESCRIPTION_MAX_LENGTH = 500
LIST_MAX_LIMIT = 500


def ensure_json_native(value: Any, path: str = "$") -> None:
    """Verifica recursivamente que ``value`` sea JSON nativo.

    Raises:
        ValueError: SI hay una clave no ``str``, un ``float`` no finito o un tipo que
            JSON no representa (``bytes``, ``set``, ``tuple``, objetos arbitrarios...).
    """
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = f"{path}: valor float no finito ({value!r}) no es JSON válido"
            raise ValueError(msg)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            ensure_json_native(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                msg = f"{path}: las claves deben ser str (recibido {type(key).__name__})"
                raise ValueError(msg)  # noqa: TRY004 - contrato de validación Pydantic
            ensure_json_native(item, f"{path}.{key}")
        return
    msg = f"{path}: tipo no soportado ({type(value).__name__}); solo int/float/bool/str/list/dict"
    raise ValueError(msg)  # noqa: TRY004 - contrato de validación Pydantic


def _check_parameters(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is not None:
        ensure_json_native(value)
    return value


class StrategyCreate(BaseModel):
    """Payload de ``create`` (RF-1, RF-7)."""

    name: str = Field(pattern=NAME_PATTERN)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_schema: dict[str, Any] | None = None  # JSON Schema draft-07
    tags: list[str] = Field(default_factory=list)

    @field_validator("parameters")
    @classmethod
    def _parameters_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _check_parameters(value)
        return value

    @field_validator("parameter_schema")
    @classmethod
    def _schema_is_json(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_parameters(value)


class StrategyUpdate(BaseModel):
    """Payload de ``update`` (RF-3): reemplazo completo XOR JSON Patch RFC 6902."""

    parameters: dict[str, Any] | None = None
    parameters_patch: list[dict[str, Any]] | None = None
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    tags: list[str] | None = None

    @field_validator("parameters")
    @classmethod
    def _parameters_are_json(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_parameters(value)

    @model_validator(mode="after")
    def check_one_of(self) -> StrategyUpdate:
        if self.parameters is not None and self.parameters_patch is not None:
            msg = (
                "Provide either 'parameters' (full replace) or 'parameters_patch' "
                "(partial), not both"
            )
            raise ValueError(msg)
        if self.parameters is None and self.parameters_patch is None:
            msg = "Must provide either 'parameters' or 'parameters_patch'"
            raise ValueError(msg)
        return self


class StrategyVersion(BaseModel):
    """Una versión inmutable. Incluye ``strategy_id`` (RF-1/RF-3 lo exigen en la respuesta)."""

    strategy_id: str
    version: int = Field(ge=1)
    parameters: dict[str, Any]
    hash: str = Field(pattern=HASH_PATTERN)  # SHA-256 hex del JSON canónico
    created_at_ms: int
    created_by: str | None = None


class Strategy(BaseModel):
    """Estrategia completa con todas sus versiones (RF-2).

    ``selected_version`` es la versión resuelta por ``get`` (la pedida o la última).
    """

    id: str  # UUID v7
    name: str
    description: str | None
    parameter_schema: dict[str, Any] | None
    tags: list[str]
    created_at_ms: int
    deleted_at_ms: int | None = None
    selected_version: int | None = None
    versions: list[StrategyVersion] = Field(default_factory=list)

    @property
    def latest_version(self) -> StrategyVersion | None:
        return max(self.versions, key=lambda v: v.version) if self.versions else None

    @property
    def selected(self) -> StrategyVersion | None:
        """Versión resuelta (``selected_version``) o la última si no se fijó."""
        if self.selected_version is None:
            return self.latest_version
        return next((v for v in self.versions if v.version == self.selected_version), None)


class StrategySummary(BaseModel):
    id: str
    name: str
    latest_version: int
    updated_at_ms: int
    tags: list[str]
    hash_latest: str
    deleted_at_ms: int | None = None


class StrategyListFilters(BaseModel):
    """Filtros de ``list`` (RF-5), compartidos por CLI y API."""

    tag: str | None = None
    name_contains: str | None = None
    include_deleted: bool = False
    limit: int = Field(default=50, ge=1, le=LIST_MAX_LIMIT)
    offset: int = Field(default=0, ge=0)


class StrategyListResponse(BaseModel):
    items: list[StrategySummary]
    total_count: int
    limit: int
    offset: int


class DiffResult(BaseModel):
    from_version: int
    to_version: int
    added: dict[str, Any]
    removed: dict[str, Any]
    changed: dict[str, tuple[Any, Any]]  # clave -> (antes, después)
