"""Verificaciones estáticas de una carpeta de estrategia (RF-11, RF-17, RF-18, plan §3.5).

- :func:`check_structure`: ``logic.py`` y ``README.md`` presentes; secciones de nivel 2
  obligatorias, en orden y no vacías; bloque ``mermaid`` de estados o flujo; tabla de
  parámetros con la cabecera de RF-17.
- :func:`check_ast`: guarda AST de todos los ``*.py`` antes de importar nada.
- :func:`check_params_table` y :func:`check_kind`: tras cargar la clase, la tabla del
  README coincide con ``Params``/``default_params`` y ``kind`` es el nombre de la carpeta.

La guarda AST es una **barrera de buenas prácticas, no un sandbox** (A23): el límite de
seguridad real es que la API nunca carga código (ADR-B10).
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from finances_core.backtest.strategies.hashing import iter_python_files

LOGIC_FILE = "logic.py"
README_FILE = "README.md"

#: Secciones obligatorias del README, en este orden (RF-17).
REQUIRED_SECTIONS = (
    "Lógica de negocio",
    "Matemática",
    "Diagrama",
    "Diccionario de parámetros",
)
TABLE_HEADER = ("Parámetro", "Tipo", "Rango", "Defecto", "Descripción")
MERMAID_KINDS = ("stateDiagram-v2", "flowchart")

#: Módulos importables por una estrategia (RF-18); se admiten también sus submódulos.
ALLOWED_MODULES = (
    "__future__",
    "polars",
    "pydantic",
    "math",
    "typing",
    "dataclasses",
    "enum",
    "functools",
    "itertools",
    "collections",
    "decimal",
    "finances_core.backtest.strategies",
)
NUMERIC_LIBRARIES = frozenset({"pandas", "numpy"})
#: Nombres de doble precisión prohibidos (RF-11, RFC-0004).
DOUBLE_NAMES = frozenset({"float64", "Float64", "double"})
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "open"})

_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^`\s]*)")


class StrategyCheckError(Exception):
    """Fallo de una verificación con el archivo y la causa (RF-17: «archivo y causa»)."""

    def __init__(self, file: str, cause: str) -> None:
        super().__init__(f"{file}: {cause}")
        self.file = file
        self.cause = cause


# --------------------------------------------------------------------- README
@dataclass(frozen=True)
class _Section:
    title: str
    lines: list[str]


def _iter_lines_outside_fences(lines: list[str]) -> Iterator[tuple[int, str, bool]]:
    """``(índice, línea, dentro_de_bloque)``; las líneas de apertura/cierre cuentan dentro."""
    fence: str | None = None
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if fence is None and match:
            fence = match.group(1)
            yield index, line, True
        elif fence is not None:
            if match and match.group(1).startswith(fence[0]) and len(match.group(1)) >= len(fence):
                fence = None
            yield index, line, True
        else:
            yield index, line, False


def _sections(text: str) -> list[_Section]:
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line, fenced in _iter_lines_outside_fences(lines):
        if not fenced and (match := _HEADING_RE.match(line)):
            starts.append((index, match.group(1)))
    bounds = [*starts, (len(lines), "")]
    return [
        _Section(title, lines[start + 1 : bounds[pos + 1][0]])
        for pos, (start, title) in enumerate(starts)
    ]


def _required_sections(text: str) -> dict[str, _Section]:
    found: dict[str, _Section] = {}
    order: list[str] = []
    for section in _sections(text):
        if section.title not in REQUIRED_SECTIONS:
            continue
        if section.title in found:
            raise StrategyCheckError(README_FILE, f"sección '## {section.title}' duplicada")
        found[section.title] = section
        order.append(section.title)
    for title in REQUIRED_SECTIONS:
        if title not in found:
            raise StrategyCheckError(README_FILE, f"falta la sección '## {title}'")
        if not any(line.strip() for line in found[title].lines):
            raise StrategyCheckError(README_FILE, f"la sección '## {title}' está vacía")
    if order != list(REQUIRED_SECTIONS):
        expected = " > ".join(REQUIRED_SECTIONS)
        raise StrategyCheckError(README_FILE, f"orden de secciones erróneo; se espera {expected}")
    return found


def _check_mermaid(section: _Section) -> None:
    lines = section.lines
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if not match or match.group(2) != "mermaid":
            continue
        body = next((item.strip() for item in lines[index + 1 :] if item.strip()), "")
        if any(body.startswith(kind) for kind in MERMAID_KINDS):
            return
        kinds = " o ".join(MERMAID_KINDS)
        raise StrategyCheckError(
            README_FILE, f"el bloque mermaid de '## Diagrama' debe ser {kinds}"
        )
    raise StrategyCheckError(README_FILE, "la sección '## Diagrama' no tiene un bloque ```mermaid")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _parameter_rows(section: _Section) -> list[list[str]]:
    lines = [line for line in section.lines if line.strip()]
    for index, line in enumerate(lines):
        if line.lstrip().startswith("|") and tuple(_cells(line)) == TABLE_HEADER:
            rows: list[list[str]] = []
            for row in lines[index + 2 :]:
                if not row.lstrip().startswith("|"):
                    break
                rows.append(_cells(row))
            return rows
    header = " | ".join(TABLE_HEADER)
    raise StrategyCheckError(
        README_FILE, f"'## Diccionario de parámetros' no tiene la tabla '{header}'"
    )


def _read_readme(folder: Path) -> dict[str, _Section]:
    path = folder / README_FILE
    if not path.is_file():
        raise StrategyCheckError(README_FILE, "falta el archivo obligatorio")
    return _required_sections(path.read_text(encoding="utf-8"))


_COMPILED_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd", ".so"})


def _check_no_compiled_modules(folder: Path) -> None:
    """Un módulo compilado fuera de ``__pycache__`` es importable pero escapa a la guarda
    AST y a ``code_hash``; se rechaza la carpeta."""
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if (
            path.suffix.lower() in _COMPILED_SUFFIXES
            and path.is_file()
            and "__pycache__" not in relative.parts[:-1]
        ):
            raise StrategyCheckError(relative.as_posix(), "módulo compilado no permitido")


def check_structure(folder: Path) -> None:
    """Estructura de carpeta y README de RF-17 (sin importar código).

    Raises:
        StrategyCheckError: SI falta ``logic.py`` o ``README.md``, hay un módulo compilado
            fuera de ``__pycache__``, falta, se repite o
            está vacía una sección obligatoria, el orden es erróneo, ``## Diagrama`` no
            tiene un bloque ``mermaid`` de estados o flujo, o falta la tabla de parámetros.
    """
    if not (folder / LOGIC_FILE).is_file():
        raise StrategyCheckError(LOGIC_FILE, "falta el archivo obligatorio")
    _check_no_compiled_modules(folder)
    sections = _read_readme(folder)
    _check_mermaid(sections["Diagrama"])
    _parameter_rows(sections["Diccionario de parámetros"])


def _strip_code(cell: str) -> str:
    return cell.strip().strip("`").strip()


def _default_matches(cell: str, expected: Any) -> bool:
    text = _strip_code(cell)
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = text
    if isinstance(expected, bool) or isinstance(parsed, bool):
        return parsed is expected
    if isinstance(expected, int | float):
        return isinstance(parsed, int | float) and float(parsed) == float(expected)
    if expected is None:
        return parsed is None or text == "None"
    return parsed == expected or text == str(expected)


def check_params_table(
    folder: Path, params_model: type[BaseModel], default_params: Mapping[str, Any]
) -> None:
    """La tabla del README tiene una fila por campo de ``Params`` con su defecto (RF-17).

    El defecto esperado es el efectivo: ``Params.model_validate(default_params)``.

    Raises:
        StrategyCheckError: SI falta o sobra una fila, se repite un parámetro, una celda
            está vacía o un defecto no coincide.
    """
    rows = _parameter_rows(_read_readme(folder)["Diccionario de parámetros"])
    defaults = params_model.model_validate(dict(default_params)).model_dump(mode="json")
    seen: set[str] = set()
    for row in rows:
        if len(row) != len(TABLE_HEADER) or not all(row):
            raise StrategyCheckError(README_FILE, f"fila de parámetros incompleta: {row}")
        name = _strip_code(row[0])
        if name in seen:
            raise StrategyCheckError(README_FILE, f"fila de parámetro '{name}' duplicada")
        seen.add(name)
        if name not in params_model.model_fields:
            raise StrategyCheckError(README_FILE, f"la fila '{name}' no es un campo de Params")
        if not _default_matches(row[3], defaults.get(name)):
            raise StrategyCheckError(
                README_FILE,
                f"'{name}': el defecto {row[3]!r} no coincide con default_params "
                f"({defaults.get(name)!r})",
            )
    missing = [field for field in params_model.model_fields if field not in seen]
    if missing:
        raise StrategyCheckError(
            README_FILE, f"faltan filas de parámetros para: {', '.join(missing)}"
        )


def check_kind(folder: Path, strategy: type) -> None:
    """``kind`` de la clase igual al nombre de la carpeta (RF-16).

    Raises:
        StrategyCheckError: SI difieren.
    """
    kind = getattr(strategy, "kind", None)
    if kind != folder.name:
        raise StrategyCheckError(
            LOGIC_FILE, f"kind {kind!r} distinto del nombre de la carpeta {folder.name!r}"
        )


# ------------------------------------------------------------------- guarda AST
def _module_allowed(name: str) -> bool:
    return any(name == allowed or name.startswith(f"{allowed}.") for allowed in ALLOWED_MODULES)


def _import_cause(name: str) -> str:
    root = name.split(".", maxsplit=1)[0]
    if root in NUMERIC_LIBRARIES:
        return f"import prohibido '{name}' (RF-11: sin pandas ni NumPy en estrategias)"
    return f"import fuera de la lista permitida '{name}' (RF-18)"


def _import_violations(node: ast.AST, depth: int) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if not _module_allowed(alias.name):
                yield _import_cause(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            if node.level > depth + 1:
                yield "import relativo fuera de la carpeta de la estrategia"
            return
        module = node.module or ""
        if not _module_allowed(module) and not all(
            _module_allowed(f"{module}.{alias.name}") for alias in node.names
        ):
            yield _import_cause(module)


def _name_violations(node: ast.AST) -> Iterator[str]:
    names: list[str] = []
    if isinstance(node, ast.Name):
        names.append(node.id)
        if node.id in FORBIDDEN_CALLS:
            yield f"uso prohibido de '{node.id}' (RF-18)"
    elif isinstance(node, ast.Attribute):
        names.append(node.attr)
    elif isinstance(node, ast.alias):
        names.extend(n for n in (node.name, node.asname) if n)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        names.append(node.value)
    for name in names:
        if name in DOUBLE_NAMES:
            yield f"nombre de doble precisión prohibido '{name}' (RF-11)"


def ast_violations(source: str, relative: str) -> list[str]:
    """Violaciones de la guarda AST en ``source`` (``relative``: ruta POSIX en la carpeta).

    Cada entrada es ``"<ruta>:<línea>: <causa>"``. Los imports relativos se admiten hasta
    la raíz de la carpeta de la estrategia (módulos hermanos), nunca por encima.
    """
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as exc:
        return [f"{relative}:{exc.lineno or 0}: error de sintaxis: {exc.msg}"]
    depth = relative.count("/")
    found: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        for cause in (*_import_violations(node, depth), *_name_violations(node)):
            found.append(f"{relative}:{line}: {cause}")
    return sorted(set(found), key=lambda item: (int(item.split(":")[1]), item))


def check_ast(folder: Path) -> None:
    """Guarda AST sobre todos los ``*.py`` de la carpeta (RF-18), antes de importar.

    Raises:
        StrategyCheckError: con el primer archivo infractor y su causa (línea incluida).
    """
    for relative, path in iter_python_files(folder):
        found = ast_violations(path.read_text(encoding="utf-8"), relative)
        if found:
            cause = found[0].removeprefix(f"{relative}:")
            raise StrategyCheckError(relative, f"línea {cause}")
