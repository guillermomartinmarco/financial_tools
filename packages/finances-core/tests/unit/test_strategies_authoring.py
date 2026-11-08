"""B1.11: plantilla ``strategies/_template`` y guía ``docs/guides/strategy-authoring.md``
(RF-22, plan §4 y §5).

- La plantilla copiada a un ``FT_STRATEGIES_DIR`` temporal y renombrada pasa
  ``assert_strategy_contract``; en su sitio, el descubrimiento la ignora.
- La guía se ejecuta tal cual contra un ``FT_STRATEGIES_DIR`` temporal: los bloques
  precedidos de ``<!-- guia:archivo RUTA -->`` se escriben en disco, los de
  ``<!-- guia:python -->`` se ejecutan en orden en un mismo espacio de nombres y los de
  ``<!-- guia:comando -->`` se lanzan línea a línea (``ft_strategy_manager``).
- Los ``README.md`` y ``_template/`` están declarados como *package data*.
"""

from __future__ import annotations

import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from finances_core.backtest.catalog import BUILTIN_DIR, build_registry, get_registry
from finances_core.backtest.strategies.testing import assert_strategy_contract
from finances_core.config import FinancesSettings
from ft_strategy_manager.cli import app as strategy_manager_app

REPO_ROOT = Path(__file__).resolve().parents[4]
GUIDE = REPO_ROOT / "docs" / "guides" / "strategy-authoring.md"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = BUILTIN_DIR / "_template"
TEMPLATE_KIND = "mi_estrategia"
BUILTIN_KINDS = (
    "bollinger_reversion",
    "donchian_breakout",
    "ema_cross",
    "high_breakout",
    "macd_trend",
    "momentum_roc",
    "rsi2_connors",
    "rsi_reversion",
    "sma_cross",
    "zscore_reversion",
)

_MARKER_RE = re.compile(r"^<!--\s*guia:(archivo|python|comando)\s*(.*?)\s*-->\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


# ------------------------------------------------------------------- plantilla
def _copy_template(local: Path, name: str) -> Path:
    target = local / name
    shutil.copytree(TEMPLATE, target, ignore=shutil.ignore_patterns("__pycache__"))
    logic = target / "logic.py"
    text = logic.read_text(encoding="utf-8")
    logic.write_bytes(text.replace(f'"{TEMPLATE_KIND}"', f'"{name}"').encode("utf-8"))
    return target


@pytest.mark.parametrize("name", [TEMPLATE_KIND, "otra_estrategia"])
def test_template_copied_and_renamed_passes_contract(tmp_path: Path, name: str) -> None:
    local = tmp_path / "strategies"
    _copy_template(local, name)
    registry = build_registry(builtin_dir=BUILTIN_DIR, local_dir=local)
    assert registry.origins[name] == "local"
    assert dict(registry.invalid) == {}
    assert_strategy_contract(name, registry)


def test_template_without_renaming_kind_is_invalid(tmp_path: Path) -> None:
    local = tmp_path / "strategies"
    shutil.copytree(TEMPLATE, local / "sin_renombrar")
    registry = build_registry(builtin_dir=BUILTIN_DIR, local_dir=local)
    assert "kind" in registry.invalid["sin_renombrar"]


def test_template_and_lib_are_not_discovered_as_strategies() -> None:
    registry = get_registry(FinancesSettings())
    assert tuple(registry.valid) == BUILTIN_KINDS
    assert TEMPLATE_KIND not in registry.valid
    assert (TEMPLATE / "logic.py").is_file()
    assert (TEMPLATE / "README.md").is_file()


# ------------------------------------------------------------------------- guía
@dataclass(frozen=True)
class GuideBlock:
    action: str  # archivo | python | comando
    argument: str
    code: str
    line: int


def guide_blocks(text: str) -> list[GuideBlock]:
    """Bloques marcados de la guía, en orden; el bloque sigue al marcador."""
    lines = text.splitlines()
    blocks: list[GuideBlock] = []
    index = 0
    while index < len(lines):
        marker = _MARKER_RE.match(lines[index])
        if marker is None:
            index += 1
            continue
        fence = _FENCE_RE.match(lines[index + 1]) if index + 1 < len(lines) else None
        assert fence is not None, f"línea {index + 1}: el marcador debe preceder a un bloque"
        start = index + 2
        end = next(
            pos
            for pos in range(start, len(lines))
            if lines[pos].strip() == fence.group(1)  # cierre con la misma valla
        )
        code = "\n".join(lines[start:end]) + "\n"
        blocks.append(GuideBlock(marker.group(1), marker.group(2), code, index + 1))
        index = end + 1
    return blocks


def test_guide_parser_handles_nested_fences() -> None:
    text = (
        "<!-- guia:archivo $FT_STRATEGIES_DIR/x/README.md -->\n"
        "````markdown\n"
        "```mermaid\nflowchart LR\n```\n"
        "````\n"
        "<!-- guia:python -->\n```python\nx = 1\n```\n"
    )
    blocks = guide_blocks(text)
    assert [b.action for b in blocks] == ["archivo", "python"]
    assert blocks[0].code == "```mermaid\nflowchart LR\n```\n"


def test_guide_has_required_content() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for heading in (
        "## Prerrequisitos",
        "## Pasos",
        "## Checklist",
        "## Boilerplate",
        "## Ejemplos",
        "## Errores frecuentes",
        "## Seguridad",
    ):
        assert heading in text, heading
    steps = re.findall(r"^(\d+)\. \*\*", text, flags=re.MULTILINE)
    assert steps[:8] == [str(n) for n in range(1, 9)]
    assert re.findall(r"^- \[ \] ", text, flags=re.MULTILINE)  # checklist
    for kind in (*BUILTIN_KINDS, "mi_momento", TEMPLATE_KIND):
        assert kind in text, kind
    assert "_template" in text
    lowered = text.lower()
    for fragment in ("shift(-1)", "pandas", "nulos", "estado entre llamadas"):
        assert fragment in lowered, fragment
    # RF-22/RF-18: nunca código por la API; A23: junctions y enlaces simbólicos se siguen
    assert "nunca" in lowered and "api" in lowered
    assert "enlaces simbólicos" in lowered and "junction" in lowered


def _run_command(line: str) -> None:
    argv = shlex.split(line)
    assert argv[0] == "ft_strategy_manager", f"comando no ejecutable en el test: {line}"
    result = CliRunner().invoke(strategy_manager_app, argv[1:])
    assert result.exit_code == 0, f"{line}\n{result.output}"


def test_guide_runs_as_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proyecto"
    workdir = tmp_path / "trabajo"
    project.mkdir()
    workdir.mkdir()
    monkeypatch.setenv("FT_PROJECT_ROOT", str(project))
    monkeypatch.setenv("FT_STRATEGIES_DIR", "./strategies")
    monkeypatch.chdir(workdir)
    strategies_dir = FinancesSettings().STRATEGIES_DIR
    assert strategies_dir == project / "strategies"

    blocks = guide_blocks(GUIDE.read_text(encoding="utf-8"))
    actions = {block.action for block in blocks}
    assert actions == {"archivo", "python", "comando"}
    namespace: dict[str, object] = {"__name__": "__guia__"}
    for block in blocks:
        where = f"{GUIDE.name}:{block.line}"
        if block.action == "archivo":
            relative = block.argument.removeprefix("$FT_STRATEGIES_DIR/")
            base = strategies_dir if relative != block.argument else workdir
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(block.code.encode("utf-8"))
        elif block.action == "python":
            exec(compile(block.code, where, "exec"), namespace)  # noqa: S102 - guía del repo
        else:
            for line in block.code.splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    _run_command(line)

    registry = get_registry(FinancesSettings())
    assert {"mi_momento", TEMPLATE_KIND} <= set(registry.valid)
    assert dict(registry.invalid) == {}
    assert (strategies_dir / "mi_momento" / "logic.py").is_file()


# ---------------------------------------------------------------- package data
def test_strategy_files_are_declared_as_package_data() -> None:
    config = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["finances_core"]
    package_dir = PACKAGE_ROOT / "src" / "finances_core"
    declared = {path for pattern in patterns for path in package_dir.glob(pattern)}
    required = [
        path
        for path in BUILTIN_DIR.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.parent != BUILTIN_DIR
        and not (path.parent / "__init__.py").is_file()  # los paquetes ya se instalan
    ]
    names = {path.relative_to(BUILTIN_DIR).as_posix() for path in required}
    assert {"_template/logic.py", "_template/README.md", "sma_cross/README.md"} <= names
    missing = sorted(p.relative_to(package_dir).as_posix() for p in required if p not in declared)
    assert missing == []
    assert (BUILTIN_DIR / "_lib" / "__init__.py").is_file()  # _lib es un paquete
