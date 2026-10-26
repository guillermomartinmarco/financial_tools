"""T0.3 de ``ft_datasets``: aislamiento Art. 4 (test AST, ADR-D01).

``ft_datasets`` invoca a los descargadores solo como subprocesos (nombres de sus *console
scripts*): no puede importar ninguna herramienta hermana ``finances_downloader.ft_*``
(tampoco con imports relativos), ni ``finances_analytics`` ni ``finances_api``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "finances_downloader" / "ft_datasets"
OWN_TOOL = "finances_downloader.ft_datasets"
FORBIDDEN_ROOTS = ("finances_analytics", "finances_api")


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
    """Imports prohibidos por el Art. 4 en ``module_name``."""
    bad: list[str] = []
    for mod in imported_modules(source, module_name, is_package=is_package):
        own = mod == OWN_TOOL or mod.startswith(f"{OWN_TOOL}.")
        sibling = mod.startswith("finances_downloader.ft_") and not own
        root = mod.split(".")[0]
        if sibling or root in FORBIDDEN_ROOTS or root.startswith("ft_"):
            bad.append(mod)
    return bad


def _modules() -> list[tuple[Path, str, bool]]:
    out: list[tuple[Path, str, bool]] = []
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        out.append((path, ".".join(["finances_downloader", *parts]), is_package))
    return out


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("from finances_downloader.ft_market_data_binance import cli", "runner"),
        ("import finances_downloader.ft_market_data_yfinance.service", "service"),
        ("from finances_downloader import ft_data_viewer", "cli"),
        ("from ..ft_market_data_binance import planner", "materializer"),
        ("from .. import ft_market_data_yfinance", "api"),
        ("from finances_analytics.ft_indicators import engine", "service"),
        ("import finances_analytics", "cli"),
        ("from finances_api.app import create_app", "api"),
        ("import ft_setup", "cli"),
    ],
)
def test_checker_detects_forbidden_imports(source: str, module: str) -> None:
    assert violations(source, f"{OWN_TOOL}.{module}")


def test_checker_allows_core_and_own_tool() -> None:
    src = (
        "import polars as pl\n"
        "from finances_core.datasets import verify\n"
        "from finances_core.schemas.datasets import DatasetRecipe\n"
        "from finances_downloader.ft_datasets import errors\n"
        "from . import runner\n"
    )
    assert violations(src, f"{OWN_TOOL}.service") == []


def test_ft_datasets_sources_respect_isolation() -> None:
    modules = _modules()
    assert {path.name for path, _, _ in modules} >= {"__init__.py", "cli.py", "api.py"}
    report: dict[str, list[str]] = {}
    for path, module_name, is_package in modules:
        bad = violations(path.read_text(encoding="utf-8"), module_name, is_package=is_package)
        if bad:
            report[str(path)] = bad
    assert report == {}
