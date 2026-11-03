"""Descubrimiento y carga de estrategias integradas y locales (RF-18, plan §3.5).

Orígenes: la carpeta de integradas (``finances_core/backtest/strategies/``) y
``FT_STRATEGIES_DIR`` (locales). Se ignoran las entradas que no son directorios o cuyo
nombre empieza por ``_`` o ``.``. Para cada carpeta, en orden de nombre: nombre válido →
colisión con una integrada → estructura y README → guarda AST → importación → una única
subclase concreta → contrato de la clase → ``kind`` = carpeta → tabla del README =
``Params`` → ``strategy_code_hash``. Cualquier fallo deja la carpeta inválida con un
mensaje legible (archivo y causa, sin traza) y el resto sigue disponible.

La carga usa ``importlib`` desde el archivo bajo un paquete sintético
``ft_strategy_<origin>_<kind>`` (permite imports relativos entre módulos hermanos) y no
modifica ``sys.path``. Importar ejecuta el nivel superior del código: el directorio local
debe ser de confianza (A23); la API nunca carga código (ADR-B10).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Literal

from finances_core.backtest.strategies.base import (
    KIND_RE,
    BaseStrategy,
    StrategyDefinitionError,
    validate_strategy_class,
)
from finances_core.backtest.strategies.checks import (
    LOGIC_FILE,
    StrategyCheckError,
    check_ast,
    check_kind,
    check_params_table,
    check_structure,
)
from finances_core.backtest.strategies.hashing import code_hash

Origin = Literal["builtin", "local"]
StrategyClass = type[BaseStrategy[Any]]

_ORIGIN_ORDER: dict[Origin, int] = {"builtin": 0, "local": 1}


@dataclass(frozen=True)
class DiscoveredStrategy:
    """Resultado de una carpeta: ``strategy`` y ``code_hash`` si es válida, o ``error``."""

    name: str  # nombre de la carpeta (== kind si es válida)
    origin: Origin
    folder: Path
    strategy: StrategyClass | None = None
    code_hash: str | None = None
    error: str | None = None


def candidate_folders(root: Path | None) -> list[Path]:
    """Subdirectorios de ``root`` candidatos a estrategia, en orden de nombre (bytes)."""
    if root is None or not root.is_dir():
        return []
    folders = [
        entry
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith(("_", "."))
    ]
    return sorted(folders, key=lambda path: path.name.encode("utf-8"))


def _purge_package(package: str) -> None:
    for name in [m for m in sys.modules if m == package or m.startswith(f"{package}.")]:
        del sys.modules[name]


def _error_location(folder: Path, tb: TracebackType | None) -> str:
    """``archivo:línea`` del último marco dentro de la carpeta (sin traza completa)."""
    location = LOGIC_FILE
    root = folder.resolve()
    while tb is not None:
        path = Path(tb.tb_frame.f_code.co_filename).resolve()
        if path.is_relative_to(root):
            location = f"{path.relative_to(root).as_posix()}:{tb.tb_lineno}"
        tb = tb.tb_next
    return location


def _import_logic(folder: Path, origin: Origin) -> ModuleType:
    package = f"ft_strategy_{origin}_{folder.name}"
    _purge_package(package)
    package_spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
    package_spec.submodule_search_locations = [str(folder)]
    sys.modules[package] = importlib.util.module_from_spec(package_spec)
    name = f"{package}.logic"
    spec = importlib.util.spec_from_file_location(name, folder / LOGIC_FILE)
    if spec is None or spec.loader is None:  # pragma: no cover - ruta .py siempre cargable
        _purge_package(package)
        msg = "no se puede crear el cargador"
        raise StrategyCheckError(LOGIC_FILE, msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:  # SystemExit del autor no tumba el registro
        _purge_package(package)
        where = _error_location(folder, exc.__traceback__)
        cause = f"error al importar: {type(exc).__name__}: {exc}"
        raise StrategyCheckError(where, cause) from None
    return module


def _single_strategy(module: ModuleType) -> StrategyClass:
    found = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, BaseStrategy)
        and obj.__module__ == module.__name__
        and not inspect.isabstract(obj)
    ]
    if len(found) != 1:
        cause = (
            "ninguna subclase concreta de BaseStrategy"
            if not found
            else f"{len(found)} subclases concretas de BaseStrategy (se exige exactamente 1)"
        )
        raise StrategyCheckError(LOGIC_FILE, cause)
    return found[0]


def _verify_and_load(folder: Path, origin: Origin) -> StrategyClass:
    """Cadena de RF-18 para una carpeta; la guarda AST precede a cualquier importación.

    Raises:
        StrategyCheckError: con el archivo y la causa del primer fallo.
    """
    if not KIND_RE.fullmatch(folder.name):
        msg = f"nombre de carpeta {folder.name!r} no cumple {KIND_RE.pattern}"
        raise StrategyCheckError(folder.name, msg)
    check_structure(folder)
    check_ast(folder)
    strategy = _single_strategy(_import_logic(folder, origin))
    try:
        validate_strategy_class(strategy)
    except StrategyDefinitionError as exc:
        raise StrategyCheckError(LOGIC_FILE, str(exc)) from None
    check_kind(folder, strategy)
    check_params_table(folder, strategy.Params, strategy.default_params)
    return strategy


def load_folder(folder: Path, origin: Origin) -> DiscoveredStrategy:
    """Verifica y carga una carpeta de estrategia; nunca propaga sus errores."""
    name = folder.name
    try:
        strategy = _verify_and_load(folder, origin)
    except StrategyCheckError as exc:
        return DiscoveredStrategy(name=name, origin=origin, folder=folder, error=str(exc))
    return DiscoveredStrategy(
        name=name, origin=origin, folder=folder, strategy=strategy, code_hash=code_hash(folder)
    )


def discover(builtin_dir: Path, local_dir: Path | None) -> list[DiscoveredStrategy]:
    """Descubre integradas y locales; orden determinista por nombre y origen (RF-18).

    Una carpeta local con el nombre de una integrada queda inválida con
    ``kind_collision`` sin importarse; la integrada nunca se sustituye.
    """
    builtins = candidate_folders(builtin_dir)
    builtin_names = {folder.name for folder in builtins}
    found = [load_folder(folder, "builtin") for folder in builtins]
    for folder in candidate_folders(local_dir):
        if folder.name in builtin_names:
            error = (
                f"kind_collision: {folder.name!r} ya es una estrategia integrada; "
                "renombra la carpeta local"
            )
            found.append(DiscoveredStrategy(folder.name, "local", folder, error=error))
        else:
            found.append(load_folder(folder, "local"))
    return sorted(found, key=lambda item: (item.name.encode("utf-8"), _ORIGIN_ORDER[item.origin]))
