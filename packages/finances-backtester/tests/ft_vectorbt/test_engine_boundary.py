"""V1.1: puntos de conversión de ``engine.py`` verificados por AST (RFC-0004, «Verificación»).

- **Ampliación** (cualquier nombre de doble precisión: ``float64``/``Float64``/``double``,
  mismo detector que ``test_isolation.py``) solo dentro de ``to_framework_input``.
- **Downcast** (los esquemas canónicos ``TRADES_SCHEMA``/``EQUITY_SCHEMA`` y cualquier
  ``astype``/``cast``/``dtype=`` que no amplíe) solo dentro de ``from_framework_output``.
- Toda llamada ``astype``/``cast`` o argumento ``dtype=`` vive en uno de los dos puntos.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ft_vectorbt.test_isolation import DOUBLE_PRECISION_NAMES

ENGINE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "finances_backtester"
    / "ft_vectorbt"
    / "engine.py"
)
WIDEN_POINT = "to_framework_input"
DOWNCAST_POINT = "from_framework_output"
CONVERSION_CALLS = frozenset({"astype", "cast"})
CANONICAL_SCHEMAS = frozenset({"TRADES_SCHEMA", "EQUITY_SCHEMA"})


def _names(node: ast.AST) -> Iterator[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            yield sub.attr
        elif isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value
        elif isinstance(sub, ast.alias):
            yield sub.name.rsplit(".", 1)[-1]


def _widens(node: ast.AST) -> bool:
    return any(name in DOUBLE_PRECISION_NAMES for name in _names(node))


def _conversions(node: ast.AST) -> Iterator[ast.AST]:
    """Llamadas ``astype``/``cast`` y argumentos ``dtype=``."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr in CONVERSION_CALLS:
                yield sub
            yield from (kw for kw in sub.keywords if kw.arg == "dtype")


def _scopes(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """``(función de nivel superior o '<módulo>', nodo)`` para cada sentencia."""
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            yield stmt.name, stmt
        elif isinstance(stmt, ast.ClassDef):
            for item in stmt.body:
                yield f"{stmt.name}.{getattr(item, 'name', '<cuerpo>')}", item
        else:
            yield "<módulo>", stmt


def boundary_violations(source: str) -> list[str]:
    """``<ámbito>:<línea>: <motivo>`` por cada conversión fuera de su punto."""
    found: list[str] = []
    for scope, node in _scopes(ast.parse(source)):
        if scope != WIDEN_POINT and _widens(node):
            found.append(f"{scope}:{getattr(node, 'lineno', 0)}: ampliación")
        if scope != DOWNCAST_POINT:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name | ast.Attribute) and (
                    (sub.id if isinstance(sub, ast.Name) else sub.attr) in CANONICAL_SCHEMAS
                ):
                    found.append(f"{scope}:{sub.lineno}: esquema canónico (downcast)")
        for conv in _conversions(node):
            line = getattr(conv, "lineno", 0)
            if scope == WIDEN_POINT:
                if not _widens(conv) and "bool_" not in set(_names(conv)):
                    found.append(f"{scope}:{line}: conversión que no amplía")
            elif scope != DOWNCAST_POINT:
                found.append(f"{scope}:{line}: conversión fuera de los puntos")
            elif _widens(conv):
                found.append(f"{scope}:{line}: ampliación en el punto de downcast")
    return found


# --------------------------------------------------------------------------- detector


@pytest.mark.parametrize(
    "source",
    [
        "def simulate(a):\n    return a.astype(np.float64)\n",
        "def simulate(a):\n    return a.astype(np.float32)\n",
        "def simulate(s):\n    return s.cast(pl.Float32)\n",
        "def simulate(v):\n    return pl.Series('x', v, dtype=pl.Float32)\n",
        "def simulate(v):\n    return TRADES_SCHEMA\n",
        "X = np.float64\n",
        "class VectorbtEngine:\n    def run(self, a):\n        return a.astype('float64')\n",
        "def to_framework_input(a):\n    return a.astype(np.float32)\n",
        "def to_framework_input(a):\n    return EQUITY_SCHEMA\n",
        "def from_framework_output(a):\n    return a.astype(np.float64)\n",
        "def from_framework_output(a):\n    return np.zeros(3, dtype=np.float64)\n",
    ],
)
def test_detector_flags_conversions_outside_their_point(source: str) -> None:
    assert boundary_violations(source)


def test_detector_allows_conversions_at_their_points() -> None:
    src = (
        "def to_framework_input(c):\n"
        "    a = c.to_numpy().astype(np.float64)\n"
        "    b = c.to_numpy().astype(np.bool_)\n"
        "    return a, b\n"
        "def from_framework_output(r):\n"
        "    s = pl.Series('x', r).cast(pl.Float32)\n"
        "    return pl.DataFrame({n: pl.Series(n, r, dtype=d) for n, d in TRADES_SCHEMA.items()})\n"
        "def simulate(inp):\n"
        "    return np.maximum.accumulate(inp.close)\n"
    )
    assert boundary_violations(src) == []


# --------------------------------------------------------------------------- engine.py


def test_engine_defines_both_conversion_points() -> None:
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    functions = {s.name for s in tree.body if isinstance(s, ast.FunctionDef)}
    assert {WIDEN_POINT, DOWNCAST_POINT, "simulate"} <= functions


def test_engine_widens_only_in_input_and_downcasts_only_in_output() -> None:
    assert boundary_violations(ENGINE.read_text(encoding="utf-8")) == []
