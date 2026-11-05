"""V0.1 y V0.3: prerrequisitos del núcleo y esqueleto de ``ft_vectorbt``."""

from __future__ import annotations

import importlib
import importlib.metadata
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from finances_backtester import ft_vectorbt
from finances_backtester.ft_vectorbt import api, cli

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_SUITE = Path(__file__).resolve().parents[1] / "contract" / "engine_contract.py"
CORE_BACKTEST_MODULES = ("catalog", "runner", "engine_api", "results_store", "metrics", "identity")


@pytest.mark.parametrize("name", CORE_BACKTEST_MODULES)
def test_core_backtest_prerequisites_import(name: str) -> None:
    """V0.1: módulos de ``finances_core.backtest`` de los que depende el motor (B5.1)."""
    importlib.import_module(f"finances_core.backtest.{name}")


def test_core_strategy_contract_exports_base_strategy_and_signals() -> None:
    strategies = importlib.import_module("finances_core.backtest.strategies")
    assert isinstance(strategies.BaseStrategy, type)
    assert isinstance(strategies.Signals, type)


def test_engine_contract_suite_exists() -> None:
    """V0.1: la suite ``engine_contract`` (B6.2) está disponible para el motor."""
    assert CONTRACT_SUITE.is_file()
    contract = importlib.import_module("tests.contract.engine_contract")
    assert callable(contract.assert_engine_matches_golden)


def test_rfc_0004_is_approved() -> None:
    """V0.1: RFC-0004 (frontera ``float64``/``pandas``) figura ``Aprobado``."""
    rfc = next((REPO_ROOT / "docs" / "rfc").glob("RFC-0004-*.md"))
    status = re.search(r"^\| Estado \| (.+?) \|$", rfc.read_text(encoding="utf-8"), re.M)
    assert status is not None
    assert status.group(1).startswith("**Aprobado**")


def test_package_metadata() -> None:
    """V0.3: ``ENGINE_NAME`` y ``__version__`` (plan §1.1)."""
    assert ft_vectorbt.ENGINE_NAME == "vectorbt"
    assert ft_vectorbt.__version__ == importlib.metadata.version("finances-backtester")


def test_router_prefix() -> None:
    """V0.3: router público montado por ``finances-api`` bajo ``/backtest/vectorbt`` (RF-7)."""
    assert api.router.prefix == "/backtest/vectorbt"


def test_engine_module_exists() -> None:
    importlib.import_module("finances_backtester.ft_vectorbt.engine")


def test_console_script_is_declared() -> None:
    """V0.3: script ``ft_vectorbt`` apunta a ``cli:app``."""
    scripts = importlib.metadata.entry_points(group="console_scripts")
    (entry,) = [ep for ep in scripts if ep.name == "ft_vectorbt"]
    assert entry.value == "finances_backtester.ft_vectorbt.cli:app"


def test_cli_help_lists_global_flags() -> None:
    """V0.3 / RF-6: ``--help`` responde y expone los flags globales."""
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "ft_vectorbt" in result.output
    for flag in ("--verbose", "-v", "--quiet", "-q", "--log-format", "--format"):
        assert flag in result.output


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--log-format", "xml"), ("--format", "csv")],
)
def test_cli_rejects_invalid_global_choice(flag: str, value: str) -> None:
    """RF-6: ``--log-format json|rich`` y ``--format table|json`` se validan al parsear."""
    result = CliRunner().invoke(cli.app, [flag, value])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert value in result.output


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--log-format", "json"),
        ("--log-format", "rich"),
        ("--format", "table"),
        ("--format", "json"),
    ],
)
def test_cli_accepts_valid_global_choice(flag: str, value: str) -> None:
    result = CliRunner().invoke(cli.app, [flag, value])
    assert "Invalid value" not in result.output
