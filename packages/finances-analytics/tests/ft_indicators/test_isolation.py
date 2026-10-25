"""T0.3: aislamiento Art. 4 (test AST) de ``finances_analytics.ft_indicators``."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "finances_analytics" / "ft_indicators"
OWN_TOOL = "finances_analytics.ft_indicators"
FORBIDDEN_ROOTS = ("finances_downloader", "finances_api", "pandas", "talib")


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
    """Imports prohibidos: hermanas ``ft_*``, ``finances_downloader``/``api``, pandas, talib."""
    bad: list[str] = []
    for mod in imported_modules(source, module_name, is_package=is_package):
        root = mod.split(".")[0]
        sibling = mod.startswith("finances_analytics.ft_") and not (
            mod == OWN_TOOL or mod.startswith(f"{OWN_TOOL}.")
        )
        if root in FORBIDDEN_ROOTS or root.startswith("ft_") or sibling:
            bad.append(mod)
    return bad


@pytest.mark.parametrize(
    "source",
    [
        "import talib",
        "import talib.abstract",
        "import pandas as pd",
        "from finances_downloader.ft_data_viewer import exporter",
        "from finances_api.app import create_app",
        "from finances_analytics.ft_ta_lib import engine",
        "from finances_analytics import ft_ta_lib",
        "from ...ft_ta_lib import catalog",
        "import ft_setup",
    ],
)
def test_checker_detects_forbidden_imports(source: str) -> None:
    assert violations(source, f"{OWN_TOOL}.kernels.windowed")


def test_checker_allows_core_and_own_tool() -> None:
    src = (
        "import polars as pl\n"
        "from finances_core.indicators_store import store\n"
        "from finances_analytics.ft_indicators import catalog\n"
        "from . import engine\n"
        "from .kernels import windowed\n"
    )
    assert violations(src, f"{OWN_TOOL}.service") == []


def test_ft_indicators_sources_respect_isolation() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert files, f"sin módulos en {SRC}"
    report: dict[str, list[str]] = {}
    for path in files:
        parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        module_name = ".".join(["finances_analytics", *parts])
        bad = violations(path.read_text(encoding="utf-8"), module_name, is_package=is_package)
        if bad:
            report[str(path)] = bad
    assert report == {}
