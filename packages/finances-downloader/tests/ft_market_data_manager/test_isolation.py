"""T7.3: aislamiento Art. 4 (test AST) de ``finances_downloader.ft_market_data_manager``.

- El manager no importa herramientas hermanas (``ft_*``) ni otros paquetes de herramientas
  (``finances_analytics``, ``finances_api``): solo ``finances_core``.
- Ninguna otra herramienta importa del manager.
- ``ft_data_viewer`` sigue siendo de solo lectura sobre el almacén: no importa el escritor Hive
  ni el planner del manager (``atomic_write``, ``write_parquet_partitioned``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "finances_downloader"
MANAGER = "ft_market_data_manager"
FORBIDDEN_FOR_MANAGER = ("finances_analytics", "finances_api")
STORE_WRITERS = frozenset({"atomic_write", "write_parquet_partitioned", "hive_writer"})


def _imports(path: Path) -> set[str]:
    """Módulos y nombres importados por un archivo (``from a.b import c`` => ``a.b`` y ``c``)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            found.update(alias.name for alias in node.names)
    return found


def _sources(tool: str) -> list[Path]:
    files = sorted((PACKAGE / tool).rglob("*.py"))
    assert files, f"no se encontraron fuentes de {tool}"
    return files


def _sibling_tools() -> list[str]:
    return sorted(
        p.name
        for p in PACKAGE.iterdir()
        if p.is_dir() and p.name.startswith("ft_") and p.name != MANAGER
    )


@pytest.mark.parametrize("source", _sources(MANAGER), ids=lambda p: p.name)
def test_manager_imports_no_sibling_tool_or_other_tool_package(source: Path) -> None:
    siblings = _sibling_tools()
    assert siblings, "debe haber herramientas hermanas que comprobar"
    for module in _imports(source):
        root = module.split(".")[0]
        assert root not in FORBIDDEN_FOR_MANAGER, f"{source.name} importa {module}"
        parts = module.split(".")
        if parts[0] == "finances_downloader" and len(parts) > 1:
            assert parts[1] not in siblings, (
                f"{source.name} importa la herramienta hermana {module}"
            )


@pytest.mark.parametrize("tool", _sibling_tools())
def test_no_other_tool_imports_the_manager(tool: str) -> None:
    for source in _sources(tool):
        for module in _imports(source):
            assert MANAGER not in module.split("."), f"{tool}/{source.name} importa {module}"


def test_data_viewer_does_not_use_the_store_writers() -> None:
    for source in _sources("ft_data_viewer"):
        used = _imports(source) & STORE_WRITERS
        assert not used, f"ft_data_viewer/{source.name} importa escritores del almacén: {used}"
