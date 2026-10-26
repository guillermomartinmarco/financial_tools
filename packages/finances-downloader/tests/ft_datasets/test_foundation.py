"""T0.2 de ``ft_datasets``: esqueleto importable, router ``/datasets`` y script de consola."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from typer.testing import CliRunner

TOOL_PACKAGE = "finances_downloader.ft_datasets"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_cli_and_router_import() -> None:
    cli = importlib.import_module(f"{TOOL_PACKAGE}.cli")
    router = importlib.import_module(f"{TOOL_PACKAGE}.api").router
    assert cli.app
    assert router.prefix == "/datasets"


def test_help_responds_with_global_flags() -> None:
    cli = importlib.import_module(f"{TOOL_PACKAGE}.cli")
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--verbose", "--quiet", "--log-format"):
        assert flag in result.output


def test_console_script_declared() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["ft_datasets"] == f"{TOOL_PACKAGE}.cli:app"


def test_tool_has_no_own_logging_module() -> None:
    tool = importlib.import_module(TOOL_PACKAGE)
    assert tool.__file__ is not None
    assert not (Path(tool.__file__).parent / "logging.py").exists()
