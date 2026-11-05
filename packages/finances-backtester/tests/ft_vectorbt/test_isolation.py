"""V0.4: aislamiento Art. 4 (test AST) de ``finances_backtester.ft_vectorbt`` y RFC-0004.

- Prohibido importar herramientas hermanas (raíces ``ft_*`` y ``finances_backtester.ft_*``
  distintas de ``ft_vectorbt``), ``finances_downloader``, ``finances_analytics`` y
  ``finances_api``.
- Prohibido importar ``finances_core.backtest.strategies`` y ``finances_core.backtest.catalog``
  (RF-21; se reutiliza ``engine_contract.engine_import_violations``).
- ``pandas``, ``numpy`` y ``vectorbt`` solo se importan en ``engine.py`` (RFC-0004).
- Nombres de doble precisión (``float64``, ``Float64``, ``double``) solo en ``engine.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.contract.engine_contract import engine_import_violations

SRC = Path(__file__).resolve().parents[2] / "src" / "finances_backtester" / "ft_vectorbt"
OWN_TOOL = "finances_backtester.ft_vectorbt"
FORBIDDEN_ROOTS = ("finances_downloader", "finances_analytics", "finances_api")
#: Bibliotecas de doble precisión confinadas a la frontera del motor (RFC-0004).
ENGINE_ONLY_ROOTS = frozenset({"pandas", "numpy", "vectorbt"})
BOUNDARY_MODULE = "engine"
DOUBLE_PRECISION_NAMES = frozenset({"float64", "Float64", "double"})


def _dynamic_import(node: ast.Call) -> str | None:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    arg = node.args[0] if node.args else None
    if name in {"import_module", "__import__"} and isinstance(arg, ast.Constant):
        return arg.value if isinstance(arg.value, str) else None
    return None


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
        elif isinstance(node, ast.Call) and (dynamic := _dynamic_import(node)):
            found.append(dynamic)
    return found


def violations(source: str, module_name: str, *, is_package: bool = False) -> list[str]:
    """Imports prohibidos en ``module_name`` (Art. 4, RF-21 y frontera RFC-0004)."""
    short = module_name.rsplit(".", 1)[-1]
    at_boundary = not is_package and short == BOUNDARY_MODULE
    bad: list[str] = []
    for mod in imported_modules(source, module_name, is_package=is_package):
        root = mod.split(".")[0]
        sibling = mod.startswith("finances_backtester.ft_") and not (
            mod == OWN_TOOL or mod.startswith(f"{OWN_TOOL}.")
        )
        misplaced = root in ENGINE_ONLY_ROOTS and not at_boundary
        if root in FORBIDDEN_ROOTS or root.startswith("ft_") or sibling or misplaced:
            bad.append(mod)
    bad.extend(engine_import_violations(source, module_name))
    return bad


def double_precision_mentions(source: str) -> list[int]:
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
        if name in DOUBLE_PRECISION_NAMES:
            lines.append(getattr(node, "lineno", 0))
    return lines


def _modules(root: Path) -> Iterator[tuple[Path, str, bool]]:
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root.parent).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        yield path, ".".join(["finances_backtester", *parts]), is_package


def scan(root: Path) -> dict[str, list[str]]:
    """Informe ``{archivo: [violaciones]}`` de imports y nombres de doble precisión."""
    report: dict[str, list[str]] = {}
    for path, module_name, is_package in _modules(root):
        source = path.read_text(encoding="utf-8")
        bad = violations(source, module_name, is_package=is_package)
        if is_package or module_name.rsplit(".", 1)[-1] != BOUNDARY_MODULE:
            bad.extend(f"double precision at line {n}" for n in double_precision_mentions(source))
        if bad:
            report[path.relative_to(root).as_posix()] = bad
    return report


# --------------------------------------------------------------------------- detector


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import ft_setup", "cli"),
        ("from ft_indicators import cli", "service"),
        ("from finances_backtester.ft_backtrader import engine", "engine"),
        ("from finances_backtester import ft_bt", "service"),
        ("from ..ft_backtesting_py import engine", "api"),
        ("from finances_downloader.ft_data_viewer import exporter", "service"),
        ("from finances_analytics.ft_indicators import catalog", "service"),
        ("import finances_analytics", "engine"),
        ("from finances_api.app import create_app", "api"),
        ("from finances_core.backtest.strategies import BaseStrategy", "engine"),
        ("from finances_core.backtest.strategies.base import Signals", "service"),
        ("from finances_core.backtest.catalog import build_registry", "service"),
        ("from finances_core.backtest import catalog", "cli"),
        ("import importlib\nimportlib.import_module('finances_core.backtest.catalog')", "api"),
        ("import pandas as pd", "service"),
        ("import numpy as np", "cli"),
        ("from numpy import float64", "api"),
        ("import vectorbt as vbt", "service"),
        ("from vectorbt.portfolio import Portfolio", "api"),
        ("__import__('pandas')", "service"),
    ],
)
def test_checker_detects_forbidden_imports(source: str, module: str) -> None:
    assert violations(source, f"{OWN_TOOL}.{module}")


def test_checker_rejects_double_precision_libraries_in_package_init() -> None:
    assert violations("import numpy as np\n", OWN_TOOL, is_package=True)


def test_checker_allows_core_own_tool_and_engine_libraries_at_boundary() -> None:
    src = (
        "import numpy as np\n"
        "import pandas as pd\n"
        "import vectorbt as vbt\n"
        "import polars as pl\n"
        "from finances_core.backtest.engine_api import EngineOutput, Signals\n"
        "from finances_core.backtest import runner, results_store\n"
        "from finances_backtester.ft_vectorbt import ENGINE_NAME\n"
        "from . import cli\n"
    )
    assert violations(src, f"{OWN_TOOL}.engine") == []


def test_checker_allows_core_and_own_tool_outside_boundary() -> None:
    src = (
        "import polars as pl\n"
        "from finances_core.backtest.runner import run_backtest\n"
        "from finances_backtester.ft_vectorbt.engine import VectorbtEngine\n"
        "from .engine import VectorbtEngine as E\n"
    )
    assert violations(src, f"{OWN_TOOL}.service") == []


@pytest.mark.parametrize(
    "source",
    [
        "x = np.float64\n",
        "y = pl.Float64\n",
        "z = 'float64'\n",
        "from numpy import float64\n",
        "w = arr.astype(double)\n",
    ],
)
def test_double_precision_detector(source: str) -> None:
    assert double_precision_mentions(source) == [1]


def test_double_precision_detector_ignores_float32_and_prose() -> None:
    src = 'x = pl.Float32\n"""Ampliación a doble precisión (float64) en engine.py."""\n'
    assert double_precision_mentions(src) == []


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("service.py", "import pandas as pd\n"),
        ("cli.py", "x = pl.Float64\n"),
        ("api.py", "from finances_analytics import ft_indicators\n"),
        ("__init__.py", "import vectorbt\n"),
    ],
)
def test_scan_reports_offending_file(tmp_path: Path, filename: str, source: str) -> None:
    package = tmp_path / "ft_vectorbt"
    package.mkdir()
    (package / "engine.py").write_text("import numpy as np\nx = np.float64\n", encoding="utf-8")
    (package / filename).write_text(source, encoding="utf-8")
    assert list(scan(package)) == [filename]


# --------------------------------------------------------------------------- fuentes reales


def test_ft_vectorbt_sources_respect_isolation() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert (SRC / "engine.py") in files, f"sin engine.py en {SRC}"
    assert scan(SRC) == {}
