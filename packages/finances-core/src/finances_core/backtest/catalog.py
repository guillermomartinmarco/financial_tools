"""Registro de estrategias (RF-1, RF-18, plan §1.3, ADR-B12).

:func:`get_registry` construye el registro una vez por proceso y origen (integradas +
``FT_STRATEGIES_DIR``) y no lo recarga en caliente: añadir o editar una estrategia local
exige reiniciar el proceso. El directorio local solo se lee de ``FinancesSettings``
(variable de entorno del proceso); ninguna petición puede indicarlo (RF-12, RF-18).

``CATALOG_VERSION`` versiona el contrato y la infraestructura común (``BaseStrategy``,
normalización de señales, ``strategies/_lib``), no cada estrategia (A22).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from finances_core.backtest import strategies
from finances_core.backtest.errors import InvalidParamsError, StrategyInitError, UnknownKindError
from finances_core.backtest.strategies.discovery import (
    DiscoveredStrategy,
    Origin,
    StrategyClass,
    discover,
)
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import KindInfo

logger = structlog.get_logger(__name__)

CATALOG_VERSION = "1.0.0"
RESULT_SCHEMA_VERSION = "1"
#: Carpeta de las estrategias integradas (``finances_core/backtest/strategies/``).
BUILTIN_DIR = Path(strategies.__file__).resolve().parent

_PARAMETERS_KEYS = ("kind", "params")


@dataclass(frozen=True)
class StrategyRegistry:
    """Estrategias válidas e inválidas, ordenadas por ``kind`` ascendente (RF-1).

    ``invalid`` solo contiene carpetas locales (una integrada inválida impide construir
    el registro); una colisión deja la local en ``invalid`` y la integrada en ``valid``.
    """

    valid: Mapping[str, StrategyClass]
    invalid: Mapping[str, str]
    origins: Mapping[str, Origin]
    code_hashes: Mapping[str, str]
    folders: Mapping[str, Path]

    def get(self, kind: str) -> StrategyClass:
        """Clase de la estrategia ``kind``.

        Raises:
            StrategyInitError: SI ``kind`` es una carpeta local inválida (RF-18).
            UnknownKindError: SI ``kind`` no está registrado (lista los válidos).
        """
        strategy = self.valid.get(kind)
        if strategy is not None:
            return strategy
        if kind in self.invalid:
            raise StrategyInitError(kind, "local", self.invalid[kind])
        raise UnknownKindError(kind, list(self.valid))

    def kinds(self) -> list[KindInfo]:
        """Fichas de válidas e inválidas en orden determinista (``kind``, integrada antes)."""
        infos = [self._valid_info(kind, strategy) for kind, strategy in self.valid.items()]
        infos.extend(
            KindInfo(
                kind=kind,
                version=None,
                origin="local",
                description=None,
                code_hash=None,
                params_schema=None,
                default_params=None,
                metrics_target=None,
                valid=False,
                error=error,
            )
            for kind, error in self.invalid.items()
        )
        return sorted(infos, key=lambda i: (i.kind.encode("utf-8"), i.origin != "builtin"))

    def _valid_info(self, kind: str, strategy: StrategyClass) -> KindInfo:
        target = strategy.metrics_target
        defaults = strategy.Params.model_validate(dict(strategy.default_params))
        return KindInfo(
            kind=kind,
            version=strategy.version,
            origin=self.origins[kind],
            description=strategy.description,
            code_hash=self.code_hashes[kind],
            params_schema=strategy.Params.model_json_schema(),
            default_params=defaults.model_dump(mode="json"),
            metrics_target=target.model_dump(mode="json") if target is not None else None,
            valid=True,
            error=None,
        )


def _log_discovery(found: list[DiscoveredStrategy]) -> None:
    logger.info(
        "strategies_discovered",
        strategies=[
            {"kind": item.name, "origin": item.origin, "code_hash": item.code_hash}
            for item in found
            if item.error is None
        ],
        invalid=[
            {"kind": item.name, "origin": item.origin, "error": item.error}
            for item in found
            if item.error is not None
        ],
    )


def build_registry(*, builtin_dir: Path, local_dir: Path | None) -> StrategyRegistry:
    """Descubre y registra las estrategias de ambos orígenes (RF-18).

    Raises:
        StrategyInitError: SI una estrategia **integrada** es inválida (defecto del
            paquete; exit 3).
    """
    found = discover(builtin_dir, local_dir)
    _log_discovery(found)
    for item in found:
        if item.origin == "builtin" and item.error is not None:
            raise StrategyInitError(item.name, "builtin", item.error)
    valid: dict[str, StrategyClass] = {}
    invalid: dict[str, str] = {}
    origins: dict[str, Origin] = {}
    hashes: dict[str, str] = {}
    folders: dict[str, Path] = {}
    for item in found:
        if item.strategy is None or item.code_hash is None:
            invalid[item.name] = item.error or "estrategia inválida"
            continue
        valid[item.name] = item.strategy
        origins[item.name] = item.origin
        hashes[item.name] = item.code_hash
        folders[item.name] = item.folder
    return StrategyRegistry(
        valid=MappingProxyType(valid),
        invalid=MappingProxyType(invalid),
        origins=MappingProxyType(origins),
        code_hashes=MappingProxyType(hashes),
        folders=MappingProxyType(folders),
    )


_cache_lock = threading.Lock()
_cache: dict[tuple[Path, Path], StrategyRegistry] = {}


def get_registry(settings: FinancesSettings) -> StrategyRegistry:
    """Registro del proceso para ``settings.STRATEGIES_DIR`` (sin recarga en caliente).

    La primera llamada por directorio construye el registro; las siguientes devuelven la
    misma instancia aunque el directorio cambie en disco (RF-18).

    Raises:
        StrategyInitError: SI una estrategia integrada es inválida.
    """
    key = (BUILTIN_DIR, settings.STRATEGIES_DIR.resolve())
    with _cache_lock:
        registry = _cache.get(key)
        if registry is None:
            registry = build_registry(builtin_dir=key[0], local_dir=key[1])
            _cache[key] = registry
    return registry


def _field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        path = ".".join(["params", *(str(part) for part in error["loc"])])
        errors.setdefault(path, error["msg"])
    return errors


def parse_strategy_parameters(
    parameters: dict[str, Any], registry: StrategyRegistry
) -> tuple[StrategyClass, BaseModel]:
    """Valida ``parameters = {"kind": ..., "params": {...}}`` contra el registro (RF-1).

    Returns:
        La clase de la estrategia y sus ``Params`` validados.

    Raises:
        InvalidParamsError: SI ``parameters`` no tiene exactamente las claves ``kind`` y
            ``params``, ``kind`` no es texto, ``params`` no es un objeto o incumple
            ``Params`` (detalle por campo).
        UnknownKindError: SI ``kind`` no está registrado.
        StrategyInitError: SI ``kind`` es una carpeta local inválida.
    """
    if not isinstance(parameters, dict):
        raise InvalidParamsError(None, {"parameters": "debe ser un objeto {kind, params}"})
    errors = {
        f"parameters.{key}": "clave no admitida"
        for key in parameters
        if key not in _PARAMETERS_KEYS
    }
    errors.update(
        {f"parameters.{key}": "obligatoria" for key in _PARAMETERS_KEYS if key not in parameters}
    )
    kind = parameters.get("kind")
    if "kind" in parameters and not isinstance(kind, str):
        errors["parameters.kind"] = "debe ser texto"
    if errors:
        raise InvalidParamsError(kind if isinstance(kind, str) else None, errors)
    assert isinstance(kind, str)  # noqa: S101 - estrechado arriba
    strategy = registry.get(kind)
    params = parameters["params"]
    if not isinstance(params, dict):
        raise InvalidParamsError(kind, {"params": "debe ser un objeto"})
    try:
        validated = strategy.Params.model_validate(params)
    except ValidationError as exc:
        raise InvalidParamsError(kind, _field_errors(exc)) from None
    return strategy, validated
