"""B1.10: guardia AST del núcleo de backtesting (RF-11, RF-15, RFC-0004, Art. 4 y 8).

``finances_core/backtest/**`` (incluidas las estrategias integradas, ``_lib`` y
``_template``) y ``finances_core/schemas/backtest.py`` no pueden:

- importar ``pandas`` ni NumPy;
- usar los nombres de doble precisión ``float64``/``Float64``/``double`` como nombre,
  atributo o alias de import (``pl.Float64``, ``from polars import Float64``…);
- importar raíces ``ft_*``, salvo ``ft_strategy_manager`` en ``strategy_source.py``
  (ADR-B02), ni ``finances_downloader``/``finances_analytics``/``finances_api``/
  ``finances_backtester``.

Las **constantes de texto** no se analizan: ``checks.py`` contiene ``"float64"``,
``"pandas"``… como datos de la guarda de estrategias, no como uso.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
BACKTEST_PKG = SRC / "finances_core" / "backtest"
SCHEMAS_MODULE = SRC / "finances_core" / "schemas" / "backtest.py"
NUMERIC_ROOTS = frozenset({"pandas", "numpy"})
DOUBLE_NAMES = frozenset({"float64", "Float64", "double"})
FORBIDDEN_ROOTS = frozenset(
    {"finances_downloader", "finances_analytics", "finances_api", "finances_backtester"}
)
#: Única excepción ``ft_*`` (ADR-B02): el envoltorio de ``ft_strategy_manager``.
FT_EXCEPTIONS = {"finances_core.backtest.strategy_source": frozenset({"ft_strategy_manager"})}


def _imported(node: ast.AST, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            parts = package.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1])
            return [f"{base}.{node.module}" if node.module else base]
        return [node.module or ""]
    return []


def _names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.alias):
        return [name for name in (node.name.rsplit(".", 1)[-1], node.asname) if name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    return []


def violations(source: str, module_name: str, *, is_package: bool = False) -> list[str]:
    """Infracciones ``"<línea>: <causa>"`` de ``source`` (``module_name`` resuelve relativos)."""
    package = module_name if is_package else module_name.rsplit(".", 1)[0]
    allowed_ft = FT_EXCEPTIONS.get(module_name, frozenset())
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        for module in _imported(node, package):
            root = module.split(".", 1)[0]
            if root in NUMERIC_ROOTS:
                found.append(f"{line}: import de {module} (RF-11)")
            elif root in FORBIDDEN_ROOTS or (root.startswith("ft_") and root not in allowed_ft):
                found.append(f"{line}: import de herramienta {module} (Art. 4)")
        found.extend(
            f"{line}: nombre de doble precisión {name} (RFC-0004)"
            for name in _names(node)
            if name in DOUBLE_NAMES
        )
    return found


def _modules() -> list[tuple[Path, str, bool]]:
    out: list[tuple[Path, str, bool]] = []
    for path in [*sorted(BACKTEST_PKG.rglob("*.py")), SCHEMAS_MODULE]:
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        out.append((path, ".".join(parts), is_package))
    return out


# --------------------------------------------------------- el detector funciona
@pytest.mark.parametrize(
    "source",
    [
        "import pandas as pd",
        "import pandas.api.types",
        "from pandas import DataFrame",
        "import numpy as np",
        "from numpy import float32",
        "import numpy.linalg",
        "x = pl.Float64",
        "s.cast(pl.Float64)",
        "from polars import Float64",
        "from polars import Float32 as float64",
        "import polars.Float64",
        "float64 = 1",
        "def f(double: int) -> None: ...",
        "y = np.double(1)",
        "import ft_setup",
        "from ft_strategy_manager import service",
        "from ft_indicators.engine import run",
        "from finances_downloader.ft_datasets import service",
        "import finances_analytics",
        "from finances_api.app import create_app",
        "from finances_backtester.ft_vectorbt import engine",
    ],
)
def test_checker_detects_violations(source: str) -> None:
    assert violations(source, "finances_core.backtest.metrics")


def test_checker_detects_violations_in_builtin_strategies() -> None:
    source = "import polars as pl\nx = pl.col('close').cast(pl.Float64)\n"
    assert violations(source, "finances_core.backtest.strategies.sma_cross.logic")


def test_ft_strategy_manager_only_allowed_in_strategy_source() -> None:
    source = "from ft_strategy_manager import errors, service\n"
    assert violations(source, "finances_core.backtest.strategy_source") == []
    assert violations(source, "finances_core.backtest.catalog")
    assert violations("import ft_setup\n", "finances_core.backtest.strategy_source")


def test_checker_ignores_string_constants_and_allows_core() -> None:
    source = (
        "import polars as pl\n"
        "from finances_core.backtest.strategies import BaseStrategy\n"
        "from . import hashing\n"
        "from ..schemas.backtest import KindInfo\n"
        'DOUBLE_NAMES = frozenset({"float64", "Float64", "double"})\n'
        'NUMERIC = ("pandas", "numpy")\n'
        "x = pl.col('close').cast(pl.Float32)\n"
    )
    assert violations(source, "finances_core.backtest.strategies.checks") == []


# --------------------------------------------------------------- código actual
def test_backtest_core_and_strategies_are_clean() -> None:
    modules = _modules()
    names = {name for _, name, _ in modules}
    assert "finances_core.backtest.strategies.checks" in names
    assert "finances_core.schemas.backtest" in names
    for kind in ("sma_cross", "rsi_reversion", "donchian_breakout", "_template"):
        assert f"finances_core.backtest.strategies.{kind}.logic" in names
    assert "finances_core.backtest.strategies._lib" in names
    report: dict[str, list[str]] = {}
    for path, module_name, is_package in modules:
        bad = violations(path.read_text(encoding="utf-8"), module_name, is_package=is_package)
        if bad:
            report[path.relative_to(SRC).as_posix()] = bad
    assert report == {}
