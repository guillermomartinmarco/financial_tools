"""B1.4: descubrimiento y registro de estrategias (RF-1, RF-18, RF-19).

Las integradas se simulan con un directorio temporal pasado a ``build_registry``; las
locales viven en un ``FT_STRATEGIES_DIR`` temporal. Nada se escribe fuera de ``tmp_path``.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest
import structlog
from backtest_helpers import README_TEMPLATE, VALID_LOGIC, VALID_TABLE, write_strategy
from structlog.testing import capture_logs

from finances_core.backtest import catalog
from finances_core.backtest.catalog import (
    BUILTIN_DIR,
    CATALOG_VERSION,
    RESULT_SCHEMA_VERSION,
    StrategyRegistry,
    build_registry,
    get_registry,
    parse_strategy_parameters,
)
from finances_core.backtest.errors import InvalidParamsError, StrategyInitError, UnknownKindError
from finances_core.backtest.strategies.hashing import code_hash
from finances_core.config import FinancesSettings


@pytest.fixture(autouse=True)
def _default_structlog() -> None:
    structlog.reset_defaults()


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    builtin = tmp_path / "builtin"
    local = tmp_path / "local"
    builtin.mkdir()
    local.mkdir()
    return builtin, local


def _registry(dirs: tuple[Path, Path]) -> StrategyRegistry:
    return build_registry(builtin_dir=dirs[0], local_dir=dirs[1])


def test_constants() -> None:
    assert CATALOG_VERSION == "1.2.0"
    assert RESULT_SCHEMA_VERSION == "1"


def test_builtin_and_local_sorted_by_kind_regardless_of_fs_order(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    builtin, local = dirs
    for name in ("zeta_builtin", "beta_builtin"):
        write_strategy(builtin, name)
    for name in ("omega_local", "alpha_local"):
        write_strategy(local, name)
    original = Path.iterdir

    def reversed_iterdir(self: Path):  # type: ignore[no-untyped-def]
        return iter(sorted(original(self), reverse=True))

    monkeypatch.setattr(Path, "iterdir", reversed_iterdir)
    registry = _registry(dirs)
    assert list(registry.valid) == ["alpha_local", "beta_builtin", "omega_local", "zeta_builtin"]
    assert [info.kind for info in registry.kinds()] == list(registry.valid)
    assert registry.origins["beta_builtin"] == "builtin"
    assert registry.origins["alpha_local"] == "local"


def test_underscore_dot_and_non_directories_are_ignored(dirs: tuple[Path, Path]) -> None:
    builtin, local = dirs
    write_strategy(builtin, "_lib", logic="import os\n")
    write_strategy(builtin, "__pycache__", logic=None, readme=None)
    write_strategy(local, ".hidden", logic="import os\n")
    write_strategy(local, "_template")
    (local / "notes.txt").write_text("x", encoding="utf-8")
    write_strategy(local, "good_one")
    registry = _registry(dirs)
    assert list(registry.valid) == ["good_one"]
    assert dict(registry.invalid) == {}


def test_local_collision_is_invalid_and_builtin_untouched(dirs: tuple[Path, Path]) -> None:
    builtin, local = dirs
    write_strategy(builtin, "shared_kind")
    marker = local / "imported.flag"
    logic = VALID_LOGIC + f"\nopen({str(marker)!r}, 'w')\n"
    write_strategy(local, "shared_kind", logic=logic)
    registry = _registry(dirs)
    assert registry.origins["shared_kind"] == "builtin"
    assert registry.folders["shared_kind"] == builtin / "shared_kind"
    assert "kind_collision" in registry.invalid["shared_kind"]
    infos = [(info.kind, info.origin, info.valid) for info in registry.kinds()]
    assert infos == [("shared_kind", "builtin", True), ("shared_kind", "local", False)]
    assert registry.get("shared_kind").kind == "shared_kind"
    assert not marker.exists()  # la carpeta en colisión nunca se importa


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"readme": None}, "README.md: falta"),
        ({"logic": None}, "logic.py: falta"),
        ({"logic": "import os\n"}, "logic.py: línea 1: import fuera de la lista"),
        ({"logic": VALID_LOGIC + "\nx = 1 / 0\n"}, "ZeroDivisionError"),
        (
            {"logic": VALID_LOGIC.replace('kind = "{kind}"', 'kind = "other_kind"  # {kind}')},
            "logic.py: kind 'other_kind' distinto del nombre de la carpeta",
        ),
        ({"logic": "x = 1\n"}, "ninguna subclase concreta"),
        ({"logic": VALID_LOGIC.replace('{{"window": 3}}', '{{"window": 0}}')}, "default_params"),
    ],
)
def test_invalid_local_is_registered_with_readable_error(
    dirs: tuple[Path, Path], kwargs: dict[str, str | None], expected: str
) -> None:
    write_strategy(dirs[1], "broken_one", **kwargs)  # type: ignore[arg-type]
    write_strategy(dirs[1], "fine_one")
    registry = _registry(dirs)
    message = registry.invalid["broken_one"]
    assert expected in message
    assert "Traceback" not in message
    assert "fine_one" in registry.valid
    info = next(i for i in registry.kinds() if i.kind == "broken_one")
    assert (info.valid, info.origin, info.error) == (False, "local", message)
    with pytest.raises(StrategyInitError, match="broken_one") as exc:
        registry.get("broken_one")
    assert exc.value.code == "strategy_init_error"
    assert exc.value.exit_code == 2
    assert exc.value.http_status == 500


def test_import_error_reports_file_and_line_in_sibling(dirs: tuple[Path, Path]) -> None:
    logic = VALID_LOGIC.replace(
        "from finances_core.backtest.strategies import BaseStrategy",
        "from finances_core.backtest.strategies import BaseStrategy\nfrom . import helpers",
    )
    write_strategy(
        dirs[1], "sibling_fail", logic=logic, extra_files={"helpers.py": "x = 1\ny = {}['k']\n"}
    )
    message = _registry(dirs).invalid["sibling_fail"]
    assert message.startswith("helpers.py:2: error al importar: KeyError")


def test_two_concrete_classes_are_rejected(dirs: tuple[Path, Path]) -> None:
    logic = VALID_LOGIC + "\n\nclass Twin(Momentum):\n    pass\n"
    write_strategy(dirs[1], "twin_kind", logic=logic)
    assert "2 subclases concretas" in _registry(dirs).invalid["twin_kind"]


def test_invalid_folder_name_is_rejected_without_import(dirs: tuple[Path, Path]) -> None:
    write_strategy(dirs[1], "Bad-Name")
    assert "nombre de carpeta" in _registry(dirs).invalid["Bad-Name"]


def test_sibling_modules_load_without_touching_sys_path(dirs: tuple[Path, Path]) -> None:
    logic = VALID_LOGIC.replace(
        "return pl.select(position_from_events(close > past, close < past)).to_series()",
        "return pl.select("
        "position_from_events(helpers.above(close, past), close < past)).to_series()",
    ).replace(
        "from finances_core.backtest.strategies import BaseStrategy",
        "from finances_core.backtest.strategies import BaseStrategy\n\nfrom . import helpers",
    )
    helpers = (
        "import polars as pl\n\n\n"
        "def above(a: pl.Series, b: pl.Series) -> pl.Series:\n    return a > b\n"
    )
    write_strategy(dirs[1], "with_helper", logic=logic, extra_files={"helpers.py": helpers})
    before = list(sys.path)
    registry = _registry(dirs)
    assert sys.path == before
    assert "with_helper" in registry.valid
    assert registry.code_hashes["with_helper"] == code_hash(dirs[1] / "with_helper")


def test_invalid_builtin_fails_registry_construction(dirs: tuple[Path, Path]) -> None:
    write_strategy(dirs[0], "bad_builtin", readme=None)
    with pytest.raises(StrategyInitError, match="bad_builtin") as exc:
        _registry(dirs)
    assert exc.value.origin == "builtin"
    assert exc.value.exit_code == 3


def test_unknown_kind_lists_valid_kinds(dirs: tuple[Path, Path]) -> None:
    write_strategy(dirs[1], "alpha_kind")
    write_strategy(dirs[1], "beta_kind")
    write_strategy(dirs[1], "broken_kind", readme=None)
    registry = _registry(dirs)
    with pytest.raises(UnknownKindError, match="alpha_kind, beta_kind") as exc:
        registry.get("nope")
    assert exc.value.valid_kinds == ["alpha_kind", "beta_kind"]
    assert (exc.value.code, exc.value.exit_code, exc.value.http_status) == ("unknown_kind", 2, 400)


def test_kind_info_schema_comes_from_params_and_no_schema_json(dirs: tuple[Path, Path]) -> None:
    folder = write_strategy(dirs[1], "schema_kind")
    (folder / "schema.json").write_text(json.dumps({"bogus": True}), encoding="utf-8")
    registry = _registry(dirs)
    strategy = registry.get("schema_kind")
    info = next(i for i in registry.kinds() if i.kind == "schema_kind")
    assert info.params_schema == strategy.Params.model_json_schema()
    assert info.default_params == {"window": 3}
    assert info.version == "1.0.0"
    assert info.description == strategy.description
    assert info.code_hash == code_hash(folder)
    assert info.metrics_target is None
    assert (info.valid, info.error) == (True, None)
    assert list(BUILTIN_DIR.rglob("schema.json")) == []


def test_strategies_discovered_event(dirs: tuple[Path, Path]) -> None:
    write_strategy(dirs[1], "logged_kind")
    write_strategy(dirs[1], "broken_kind", readme=None)
    with capture_logs() as logs:
        _registry(dirs)
    event = next(e for e in logs if e["event"] == "strategies_discovered")
    assert event["log_level"] == "info"
    assert event["strategies"] == [
        {
            "kind": "logged_kind",
            "origin": "local",
            "code_hash": code_hash(dirs[1] / "logged_kind"),
        }
    ]
    assert event["invalid"] == [
        {
            "kind": "broken_kind",
            "origin": "local",
            "error": "README.md: falta el archivo obligatorio",
        }
    ]


# --------------------------------------------------------- parse_strategy_parameters
def test_parse_strategy_parameters_validates_params(dirs: tuple[Path, Path]) -> None:
    write_strategy(dirs[1], "param_kind")
    registry = _registry(dirs)
    strategy, params = parse_strategy_parameters(
        {"kind": "param_kind", "params": {"window": 5}}, registry
    )
    assert strategy is registry.get("param_kind")
    assert params.model_dump() == {"window": 5}


@pytest.mark.parametrize(
    ("parameters", "field"),
    [
        ({"kind": "param_kind", "params": {"window": 1}}, "params.window"),
        ({"kind": "param_kind", "params": {"window": "x"}}, "params.window"),
        ({"kind": "param_kind", "params": {"other": 1}}, "params.other"),
        ({"kind": "param_kind", "params": [1]}, "params"),
        ({"kind": "param_kind"}, "parameters.params"),
        ({"params": {}}, "parameters.kind"),
        ({"kind": 3, "params": {}}, "parameters.kind"),
        ({"kind": "param_kind", "params": {}, "strategies_dir": "/x"}, "parameters.strategies_dir"),
        ({"kind": "param_kind", "params": {}, "code": "print(1)"}, "parameters.code"),
    ],
)
def test_parse_strategy_parameters_reports_invalid_params_by_field(
    dirs: tuple[Path, Path], parameters: dict[str, object], field: str
) -> None:
    write_strategy(dirs[1], "param_kind")
    with pytest.raises(InvalidParamsError) as exc:
        parse_strategy_parameters(parameters, _registry(dirs))
    assert field in exc.value.errors
    assert (exc.value.code, exc.value.exit_code, exc.value.http_status) == (
        "invalid_params",
        2,
        400,
    )


def test_parse_strategy_parameters_reports_every_bad_field(dirs: tuple[Path, Path]) -> None:
    logic = VALID_LOGIC.replace(
        "    window: int = Field(default=3, ge=2, le=100)",
        "    window: int = Field(default=3, ge=2, le=100)\n"
        "    ratio: float = Field(default=0.5, gt=0, lt=1)",
    )
    readme_table_row = "| `ratio` | real | (0, 1) | 0.5 | Fracción |\n"
    readme = README_TEMPLATE.format(table=VALID_TABLE + readme_table_row)
    write_strategy(dirs[1], "two_fields", logic=logic, readme=readme)
    with pytest.raises(InvalidParamsError) as exc:
        parse_strategy_parameters(
            {"kind": "two_fields", "params": {"window": 0, "ratio": 2.0}}, _registry(dirs)
        )
    assert set(exc.value.errors) == {"params.window", "params.ratio"}


def test_parse_strategy_parameters_unknown_and_invalid_kind(dirs: tuple[Path, Path]) -> None:
    write_strategy(dirs[1], "broken_kind", readme=None)
    registry = _registry(dirs)
    with pytest.raises(UnknownKindError):
        parse_strategy_parameters({"kind": "missing", "params": {}}, registry)
    with pytest.raises(StrategyInitError):
        parse_strategy_parameters({"kind": "broken_kind", "params": {}}, registry)


# ------------------------------------------------------------------- get_registry
def test_get_registry_uses_ft_strategies_dir_and_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "my_strategies"
    write_strategy(local, "cached_kind")
    monkeypatch.setenv("FT_STRATEGIES_DIR", "my_strategies")  # relativo a FT_PROJECT_ROOT
    first = get_registry(FinancesSettings())
    assert "cached_kind" in first.valid
    assert first.folders["cached_kind"] == local / "cached_kind"
    (local / "cached_kind" / "logic.py").write_text("x = 1\n", encoding="utf-8")
    write_strategy(local, "late_kind")
    second = get_registry(FinancesSettings())
    assert second is first  # sin recarga en caliente
    assert "late_kind" not in second.valid


def test_get_registry_without_local_dir_has_only_builtins(tmp_path: Path) -> None:
    registry = get_registry(FinancesSettings())
    assert all(origin == "builtin" for origin in registry.origins.values())
    assert dict(registry.invalid) == {}


def test_strategies_dir_is_not_accepted_from_requests() -> None:
    assert list(inspect.signature(get_registry).parameters) == ["settings"]
    assert list(inspect.signature(parse_strategy_parameters).parameters) == [
        "parameters",
        "registry",
    ]
    assert not hasattr(catalog, "set_strategies_dir")


def test_system_exit_at_import_marks_only_that_folder_invalid(dirs: tuple[Path, Path]) -> None:
    write_strategy(dirs[1], "exit_kind", logic="raise SystemExit(7)\n")
    write_strategy(dirs[1], "momentum")
    registry = _registry(dirs)
    assert "SystemExit" in registry.invalid["exit_kind"]
    assert "momentum" in registry.valid
