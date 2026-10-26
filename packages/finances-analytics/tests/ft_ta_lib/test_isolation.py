"""T0.3: aislamiento Art. 4 (test AST) de ``finances_analytics.ft_ta_lib`` y RFC-0002.

- Prohibido importar herramientas hermanas (``finances_analytics.ft_indicators``, ``ft_*``),
  ``finances_downloader``, ``finances_api`` y ``pandas``.
- ``talib`` solo se importa en ``engine.py`` (cálculo) y en ``catalog.py``/``resolver.py``
  (metadatos).
- ``float64`` (``np.float64``, ``pl.Float64`` o el literal ``"float64"``) solo aparece en
  ``engine.py`` (frontera RFC-0002).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "finances_analytics" / "ft_ta_lib"
OWN_TOOL = "finances_analytics.ft_ta_lib"
FORBIDDEN_ROOTS = ("finances_downloader", "finances_api", "pandas")
TALIB_MODULES = frozenset({"engine", "catalog", "resolver"})
FLOAT64_MODULES = frozenset({"engine"})
_FLOAT64_NAMES = frozenset({"float64", "Float64", "double"})


def imported_modules(source: str, module_name: str, *, is_package: bool = False) -> list[str]:
    """Módulos importados (absolutos; los relativos se resuelven contra ``module_name``)."""
    package = module_name if is_package else module_name.rsplit(".", 1)[0]
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.append(target)
            found.extend(f"{target}.{alias.name}" for alias in node.names)
    return found


def violations(source: str, module_name: str, *, is_package: bool = False) -> list[str]:
    """Imports prohibidos en ``module_name`` (``talib`` solo en motor y metadatos)."""
    short = module_name.rsplit(".", 1)[-1]
    bad: list[str] = []
    for mod in imported_modules(source, module_name, is_package=is_package):
        root = mod.split(".")[0]
        sibling = mod.startswith("finances_analytics.ft_") and not (
            mod == OWN_TOOL or mod.startswith(f"{OWN_TOOL}.")
        )
        talib_misplaced = root == "talib" and (is_package or short not in TALIB_MODULES)
        if root in FORBIDDEN_ROOTS or root.startswith("ft_") or sibling or talib_misplaced:
            bad.append(mod)
    return bad


def float64_mentions(source: str) -> list[int]:
    """Líneas que nombran ``float64``/``Float64``/``double`` (atributo, nombre o literal)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        name: object = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value
        elif isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
        if name in _FLOAT64_NAMES:
            lines.append(getattr(node, "lineno", 0))
    return lines


def _modules() -> list[tuple[Path, str, bool]]:
    out: list[tuple[Path, str, bool]] = []
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        out.append((path, ".".join(["finances_analytics", *parts]), is_package))
    return out


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import pandas as pd", "service"),
        ("from finances_downloader.ft_data_viewer import exporter", "service"),
        ("from finances_api.app import create_app", "api"),
        ("from finances_analytics.ft_indicators import engine", "engine"),
        ("from finances_analytics import ft_indicators", "catalog"),
        ("from ..ft_indicators import catalog", "resolver"),
        ("import ft_setup", "cli"),
        ("import talib", "service"),
        ("import talib.abstract", "planner"),
        ("from talib import abstract", "api"),
    ],
)
def test_checker_detects_forbidden_imports(source: str, module: str) -> None:
    assert violations(source, f"{OWN_TOOL}.{module}")


@pytest.mark.parametrize("module", sorted(TALIB_MODULES))
def test_checker_allows_core_own_tool_and_talib_where_declared(module: str) -> None:
    src = (
        "import numpy as np\n"
        "import polars as pl\n"
        "import talib\n"
        "from talib import abstract\n"
        "from finances_core.indicators_store import store\n"
        "from finances_analytics.ft_ta_lib import errors\n"
        "from . import model\n"
    )
    assert violations(src, f"{OWN_TOOL}.{module}") == []


def test_float64_detector() -> None:
    assert float64_mentions("x = np.float64\ny = pl.Float64\nz = 'float64'\n") == [1, 2, 3]
    assert float64_mentions("from numpy import float64\n") == [1]
    assert float64_mentions("x = pl.Float32\n") == []


def test_ft_ta_lib_sources_respect_isolation() -> None:
    modules = _modules()
    assert modules, f"sin módulos en {SRC}"
    report: dict[str, list[str]] = {}
    for path, module_name, is_package in modules:
        bad = violations(path.read_text(encoding="utf-8"), module_name, is_package=is_package)
        if bad:
            report[str(path)] = bad
    assert report == {}


def test_float64_only_appears_in_engine() -> None:
    """RFC-0002: la frontera ``float64`` queda aislada en ``engine.py``."""
    report: dict[str, list[int]] = {}
    for path, module_name, _is_package in _modules():
        if module_name.rsplit(".", 1)[-1] in FLOAT64_MODULES:
            continue
        lines = float64_mentions(path.read_text(encoding="utf-8"))
        if lines:
            report[str(path)] = lines
    assert report == {}
