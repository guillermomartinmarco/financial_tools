"""B1.3: verificaciones estáticas de carpeta, README y guarda AST (RF-11, RF-17, RF-18)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import polars as pl
import pytest
from backtest_helpers import README_TEMPLATE, VALID_README, VALID_TABLE, write_strategy
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy, Signals
from finances_core.backtest.strategies.checks import (
    StrategyCheckError,
    ast_violations,
    check_ast,
    check_kind,
    check_params_table,
    check_structure,
)


# ------------------------------------------------------------------ estructura
def test_valid_folder_passes_structure(tmp_path: Path) -> None:
    check_structure(write_strategy(tmp_path, "momentum"))


@pytest.mark.parametrize("missing", ["logic.py", "README.md"])
def test_missing_required_file_reports_file(tmp_path: Path, missing: str) -> None:
    folder = write_strategy(
        tmp_path,
        "momentum",
        logic=None if missing == "logic.py" else "x = 1\n",
        readme=None if missing == "README.md" else VALID_README,
    )
    with pytest.raises(StrategyCheckError) as info:
        check_structure(folder)
    assert info.value.file == missing
    assert "falta" in info.value.cause
    assert str(info.value).startswith(f"{missing}: ")


@pytest.mark.parametrize(
    "section", ["Lógica de negocio", "Matemática", "Diagrama", "Diccionario de parámetros"]
)
def test_missing_section_reports_readme_and_section(tmp_path: Path, section: str) -> None:
    readme = VALID_README.replace(f"## {section}\n", "### Otra cosa\n")
    with pytest.raises(StrategyCheckError, match=section) as info:
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))
    assert info.value.file == "README.md"


def test_empty_section_is_rejected(tmp_path: Path) -> None:
    readme = VALID_README.replace(
        "Compra cuando el cierre supera el de hace `window` barras.\n", "   \n"
    )
    with pytest.raises(StrategyCheckError, match=r"Lógica de negocio.*vacía"):
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_sections_in_wrong_order_are_rejected(tmp_path: Path) -> None:
    readme = (
        "# X\n\n## Matemática\n\nf\n\n## Lógica de negocio\n\nl\n\n"
        "## Diagrama\n\n```mermaid\nflowchart LR\n  a --> b\n```\n\n"
        f"## Diccionario de parámetros\n\n{VALID_TABLE}"
    )
    with pytest.raises(StrategyCheckError, match="orden"):
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_duplicate_required_section_is_rejected(tmp_path: Path) -> None:
    readme = VALID_README + "\n## Matemática\n\notra vez\n"
    with pytest.raises(StrategyCheckError, match="duplicada"):
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_heading_inside_code_fence_is_not_a_section(tmp_path: Path) -> None:
    readme = VALID_README.replace("## Matemática\n", "```text\n## Matemática\n```\n")
    with pytest.raises(StrategyCheckError, match="Matemática"):
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_extra_sections_are_allowed(tmp_path: Path) -> None:
    readme = VALID_README.replace("## Matemática\n", "## Ejemplo\n\nalgo\n\n## Matemática\n")
    check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_diagram_without_mermaid_block_is_rejected(tmp_path: Path) -> None:
    readme = VALID_README.replace("```mermaid\n", "```text\n")
    with pytest.raises(StrategyCheckError, match="mermaid"):
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_mermaid_block_must_be_state_or_flow_diagram(tmp_path: Path) -> None:
    readme = VALID_README.replace("stateDiagram-v2", "pie")
    with pytest.raises(StrategyCheckError, match="stateDiagram-v2"):
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_flowchart_mermaid_block_is_accepted(tmp_path: Path) -> None:
    readme = VALID_README.replace("stateDiagram-v2", "flowchart LR")
    check_structure(write_strategy(tmp_path, "momentum", readme=readme))


def test_missing_parameter_table_is_rejected(tmp_path: Path) -> None:
    readme = README_TEMPLATE.format(table="Sin tabla.\n")
    with pytest.raises(StrategyCheckError, match="tabla"):
        check_structure(write_strategy(tmp_path, "momentum", readme=readme))


# ------------------------------------------------------------ tabla vs Params / kind
class TableParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: int = Field(default=3, ge=2, le=100)


class TwoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: int = 3
    ratio: float = 0.5
    flag: bool = True
    mode: str = "fast"


TWO_TABLE = (
    "| Parámetro | Tipo | Rango | Defecto | Descripción |\n"
    "|---|---|---|---|---|\n"
    "| `window` | entero | 2..100 | 3 | a |\n"
    "| `ratio` | real | (0, 1) | `0.5` | b |\n"
    "| `flag` | booleano | — | true | c |\n"
    "| `mode` | texto | fast, slow | fast | d |\n"
)


def test_table_matching_params_passes(tmp_path: Path) -> None:
    check_params_table(write_strategy(tmp_path, "momentum"), TableParams, {"window": 3})


def test_table_with_all_scalar_types_passes(tmp_path: Path) -> None:
    folder = write_strategy(tmp_path, "momentum", readme=README_TEMPLATE.format(table=TWO_TABLE))
    check_params_table(folder, TwoParams, {"window": 3})


def test_table_missing_a_field_is_rejected(tmp_path: Path) -> None:
    folder = write_strategy(tmp_path, "momentum")
    with pytest.raises(StrategyCheckError, match="ratio") as info:
        check_params_table(folder, TwoParams, {})
    assert info.value.file == "README.md"


def test_table_with_unknown_row_is_rejected(tmp_path: Path) -> None:
    table = VALID_TABLE + "| `extra` | entero | 1..2 | 1 | x |\n"
    folder = write_strategy(tmp_path, "momentum", readme=README_TEMPLATE.format(table=table))
    with pytest.raises(StrategyCheckError, match="extra"):
        check_params_table(folder, TableParams, {"window": 3})


def test_table_with_wrong_default_is_rejected(tmp_path: Path) -> None:
    table = VALID_TABLE.replace("| 3 |", "| 4 |")
    folder = write_strategy(tmp_path, "momentum", readme=README_TEMPLATE.format(table=table))
    with pytest.raises(StrategyCheckError, match=r"window.*defecto"):
        check_params_table(folder, TableParams, {"window": 3})


def test_table_default_is_compared_with_effective_default_params(tmp_path: Path) -> None:
    folder = write_strategy(tmp_path, "momentum")
    with pytest.raises(StrategyCheckError, match="defecto"):
        check_params_table(folder, TableParams, {"window": 7})


def test_table_with_duplicate_row_is_rejected(tmp_path: Path) -> None:
    table = VALID_TABLE + "| `window` | entero | 2..100 | 3 | otra |\n"
    folder = write_strategy(tmp_path, "momentum", readme=README_TEMPLATE.format(table=table))
    with pytest.raises(StrategyCheckError, match="duplicada"):
        check_params_table(folder, TableParams, {"window": 3})


class _KindProbe(BaseStrategy[TableParams]):
    kind = "other_kind"
    version = "1.0.0"
    description = "x"
    Params = TableParams
    default_params: ClassVar[dict[str, int]] = {"window": 3}

    @staticmethod
    def warmup(params: TableParams) -> int:
        return params.window

    @staticmethod
    def signals(candles: pl.DataFrame, params: TableParams) -> Signals:
        raise NotImplementedError


def test_kind_different_from_folder_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StrategyCheckError, match="other_kind") as info:
        check_kind(write_strategy(tmp_path, "momentum"), _KindProbe)
    assert info.value.file == "logic.py"
    check_kind(write_strategy(tmp_path, "other_kind"), _KindProbe)


# ------------------------------------------------------------------- guarda AST
@pytest.mark.parametrize(
    ("source", "cause"),
    [
        ("import pandas as pd\n", "pandas"),
        ("import numpy as np\n", "numpy"),
        ("from numpy import float32\n", "numpy"),
        ("import os\n", "os"),
        ("import subprocess\n", "subprocess"),
        ("import socket\n", "socket"),
        ("import importlib\n", "importlib"),
        ("from os import path\n", "os"),
        ("import polars as pl\nx = pl.Float64\n", "Float64"),
        ("from polars import Float64\n", "Float64"),
        ("y = float64\n", "float64"),
        ("import polars as pl\ns = pl.Series([1]).cast('double')\n", "double"),
        ("eval('1')\n", "eval"),
        ("exec('x = 1')\n", "exec"),
        ("compile('1', 'f', 'eval')\n", "compile"),
        ("__import__('os')\n", "__import__"),
        ("open('f')\n", "open"),
        ("f = eval\n", "eval"),
        ("from .. import other\n", "fuera de la carpeta"),
    ],
)
def test_ast_guard_rejects_forbidden_code(source: str, cause: str) -> None:
    found = ast_violations(source, "logic.py")
    assert found, source
    assert any(cause in v for v in found), found
    assert all(v.startswith("logic.py:") for v in found)


def test_ast_guard_accepts_allowed_imports() -> None:
    source = (
        "from __future__ import annotations\n"
        "import math\n"
        "import polars as pl\n"
        "from pydantic import BaseModel, ConfigDict, Field\n"
        "from typing import ClassVar\n"
        "from dataclasses import dataclass\n"
        "from enum import Enum\n"
        "from functools import reduce\n"
        "import itertools\n"
        "from collections.abc import Mapping\n"
        "from decimal import Decimal\n"
        "from finances_core.backtest.strategies import BaseStrategy, Signals\n"
        "from finances_core.backtest.strategies._lib import sma\n"
        "from finances_core.backtest import strategies\n"
        "from . import helpers\n"
        "from .helpers import cross\n"
        "x = pl.Float32\n"
    )
    assert ast_violations(source, "logic.py") == []


def test_ast_guard_rejects_other_finances_core_modules() -> None:
    assert ast_violations("from finances_core.config import settings\n", "logic.py")
    assert ast_violations("from finances_core.backtest import catalog\n", "logic.py")


def test_ast_guard_allows_relative_import_up_to_strategy_root() -> None:
    assert ast_violations("from .. import logic\n", "sub/mod.py") == []
    assert ast_violations("from ... import x\n", "sub/mod.py")


def test_ast_guard_reports_syntax_errors() -> None:
    found = ast_violations("def broken(:\n", "logic.py")
    assert found
    assert "sintaxis" in found[0]


def test_check_ast_scans_every_python_file(tmp_path: Path) -> None:
    folder = write_strategy(tmp_path, "momentum", extra_files={"sub/helper.py": "import os\n"})
    with pytest.raises(StrategyCheckError, match="os") as info:
        check_ast(folder)
    assert info.value.file == "sub/helper.py"


def test_check_ast_passes_for_valid_folder(tmp_path: Path) -> None:
    check_ast(write_strategy(tmp_path, "momentum", extra_files={"helpers.py": "import math\n"}))


def test_check_ast_ignores_pycache(tmp_path: Path) -> None:
    folder = write_strategy(tmp_path, "momentum", extra_files={"__pycache__/x.py": "import os\n"})
    check_ast(folder)


@pytest.mark.parametrize("compiled", ["helper.pyc", "helper.pyo", "helper.pyd", "sub/helper.so"])
def test_compiled_module_outside_pycache_is_rejected(tmp_path: Path, compiled: str) -> None:
    folder = write_strategy(tmp_path, "momentum")
    target = folder / compiled
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\0")
    with pytest.raises(StrategyCheckError, match="módulo compilado"):
        check_structure(folder)


def test_compiled_module_inside_pycache_is_allowed(tmp_path: Path) -> None:
    folder = write_strategy(tmp_path, "momentum")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "logic.cpython-314.pyc").write_bytes(b"\0")
    check_structure(folder)
