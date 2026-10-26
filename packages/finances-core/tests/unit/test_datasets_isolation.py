"""T0.3 de ``ft_datasets``: ``finances_core.datasets`` no importa herramientas (Art. 4).

El núcleo es el único puente entre herramientas: ni ``finances_core.datasets`` ni
``finances_core.schemas.datasets`` pueden importar ``finances_downloader`` (ni
``finances_analytics``/``finances_api``), tampoco con imports relativos.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
DATASETS_PKG = SRC / "finances_core" / "datasets"
SCHEMAS_MODULE = SRC / "finances_core" / "schemas" / "datasets.py"
FORBIDDEN_ROOTS = ("finances_downloader", "finances_analytics", "finances_api")


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
    return found


def violations(source: str, module_name: str, *, is_package: bool = False) -> list[str]:
    return [
        mod
        for mod in imported_modules(source, module_name, is_package=is_package)
        if mod.split(".")[0] in FORBIDDEN_ROOTS or mod.split(".")[0].startswith("ft_")
    ]


def _modules() -> list[tuple[Path, str, bool]]:
    out: list[tuple[Path, str, bool]] = []
    for path in [*sorted(DATASETS_PKG.rglob("*.py")), SCHEMAS_MODULE]:
        if not path.exists():
            continue
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        out.append((path, ".".join(parts), is_package))
    return out


@pytest.mark.parametrize(
    "source",
    [
        "from finances_downloader.ft_datasets import service",
        "import finances_downloader",
        "from finances_downloader import ft_market_data_binance",
        "from finances_analytics.ft_indicators import engine",
        "from finances_api.app import create_app",
        "import ft_setup",
    ],
)
def test_checker_detects_forbidden_imports(source: str) -> None:
    assert violations(source, "finances_core.datasets.verify")


def test_checker_allows_core_imports() -> None:
    src = (
        "import polars as pl\n"
        "from finances_core.marketdata.inventory import partition_base_dir\n"
        "from . import hashing\n"
        "from ..schemas.datasets import DatasetRecipe\n"
    )
    assert violations(src, "finances_core.datasets.verify") == []


def test_core_datasets_never_imports_tools() -> None:
    modules = _modules()
    assert any(name == "finances_core.datasets" for _, name, _ in modules)
    report: dict[str, list[str]] = {}
    for path, module_name, is_package in modules:
        bad = violations(path.read_text(encoding="utf-8"), module_name, is_package=is_package)
        if bad:
            report[str(path)] = bad
    assert report == {}
