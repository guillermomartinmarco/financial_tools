"""T4.4 — Aislamiento Art. 4 y ADR-Y02 (pandas/yfinance sólo en ``adapter.py``)."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import finances_downloader.ft_market_data_yfinance as tool

TOOL_DIR = Path(tool.__file__).parent
TOOL_PACKAGE = "finances_downloader.ft_market_data_yfinance"
EDGE_LIBRARIES = ("yfinance", "pandas")


def _imports(source: Path) -> list[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def _modules() -> list[Path]:
    modules = sorted(TOOL_DIR.glob("*.py"))
    assert {p.name for p in modules} >= {
        "adapter.py",
        "timeframes.py",
        "planner.py",
        "normalizer.py",
        "events.py",
        "service.py",
        "api.py",
        "cli.py",
    }
    return modules


def test_no_cross_imports_to_sibling_tools() -> None:
    """Art. 4: sólo ``finances_core`` y el propio paquete; ningún otro ``ft_*``."""
    offenders = [
        f"{source.name}: {name}"
        for source in _modules()
        for name in _imports(source)
        if (name.startswith("finances_downloader") and not name.startswith(TOOL_PACKAGE))
        or name.startswith(("finances_api", "finances_backtester", "finances_setup"))
        or ("ft_" in name and not name.startswith(TOOL_PACKAGE))
    ]
    assert offenders == []


def test_only_adapter_imports_yfinance_or_pandas() -> None:
    offenders = [
        f"{source.name}: {name}"
        for source in _modules()
        if source.name != "adapter.py"
        for name in _imports(source)
        if name.split(".")[0] in EDGE_LIBRARIES
    ]
    assert offenders == []
    assert {n.split(".")[0] for n in _imports(TOOL_DIR / "adapter.py")} >= set(EDGE_LIBRARIES)


def test_cli_entrypoint_and_router_import() -> None:
    assert importlib.import_module(f"{TOOL_PACKAGE}.cli").app
    router = importlib.import_module(f"{TOOL_PACKAGE}.api").router
    assert router.prefix == "/market-data/yfinance"


def test_tool_has_no_own_logging_module() -> None:
    assert not (TOOL_DIR / "logging.py").exists()
