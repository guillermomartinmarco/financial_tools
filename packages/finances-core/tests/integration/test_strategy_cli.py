"""Tests de integración del CLI ``ft_strategy_manager`` (T4.1-T4.5, T6.4).

Base temporal real aislada por ``FT_PROJECT_ROOT`` (conftest). Los datos salen por stdout
y los logs por stderr, así que el JSON se lee de ``result.stdout``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml
from typer.testing import CliRunner, Result

from ft_strategy_manager import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _default_structlog() -> None:
    structlog.reset_defaults()


def _write(tmp_path: Path, name: str, content: Any) -> Path:
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def _run(*args: str, input_: str | None = None) -> Result:
    return runner.invoke(cli.app, ["--quiet", *args], input=input_)


def _create(tmp_path: Path, name: str = "alpha", params: Any = None, *extra: str) -> dict[str, Any]:
    params_file = _write(tmp_path, f"{name}.json", params or {"window": 10, "mode": "a"})
    result = _run("create", name, "--params", str(params_file), "--format", "json", *extra)
    assert result.exit_code == cli.EXIT_OK, result.output
    created: dict[str, Any] = json.loads(result.stdout)
    return created


# ----------------------------------------------------------------------- help / flags
def test_help_lists_all_subcommands_with_descriptions() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("create", "get", "update", "delete", "list", "diff", "versions", "export"):
        assert command in result.output
    assert "--verbose" in result.output
    assert "--quiet" in result.output
    assert "--log-format" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(cli.app, [])
    assert "Usage" in result.output


@pytest.mark.parametrize(
    "command", ["create", "get", "update", "delete", "list", "diff", "versions", "export"]
)
def test_every_subcommand_has_help(command: str) -> None:
    result = runner.invoke(cli.app, [command, "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_log_format_json_emits_audit_on_stderr_only(tmp_path: Path) -> None:
    params = _write(tmp_path, "p.json", {"w": 1})
    result = runner.invoke(
        cli.app, ["--log-format", "json", "create", "alpha", "--params", str(params)]
    )
    assert result.exit_code == 0
    audit = next(
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.startswith("{") and "strategy_audit" in line
    )
    assert audit["component"] == "ft_strategy_manager"
    assert audit["action"] == "create"
    assert "strategy_audit" not in result.stdout


def test_quiet_hides_info_audit_and_verbose_shows_it(tmp_path: Path) -> None:
    params = _write(tmp_path, "p.json", {"w": 1})
    quiet = runner.invoke(cli.app, ["-q", "create", "aaa", "-p", str(params)])
    verbose = runner.invoke(cli.app, ["-v", "create", "bbb", "-p", str(params)])
    assert "strategy_audit" not in quiet.stderr
    assert "strategy_audit" in verbose.stderr


# -------------------------------------------------------------------------- happy path
def test_create_table_and_json_output(tmp_path: Path) -> None:
    params = _write(tmp_path, "p.json", {"window": 5})
    table = _run("create", "alpha", "--params", str(params), "--desc", "d", "-t", "fx", "-t", "x")
    assert table.exit_code == 0
    assert "strategy_id" in table.stdout
    assert "version" in table.stdout

    created = _create(tmp_path, "beta", {"window": 5}, "--user", "ana", "--tag", "fx")
    assert created["version"] == 1
    assert created["created_by"] == "ana"
    assert len(created["hash"]) == 64
    assert created["created_at_ms"] > 0


def test_create_with_schema_file_enforces_it(tmp_path: Path) -> None:
    schema = _write(tmp_path, "s.json", {"type": "object", "required": ["window"]})
    params = _write(tmp_path, "p.json", {"mode": "a"})
    result = _run("create", "alpha", "-p", str(params), "-s", str(schema))
    assert result.exit_code == cli.EXIT_VALIDATION
    assert "window" in result.stderr


def test_get_formats_and_version_selection(tmp_path: Path) -> None:
    created = _create(tmp_path)
    sid = created["strategy_id"]
    patch = _write(tmp_path, "patch.json", [{"op": "replace", "path": "/window", "value": 99}])
    assert _run("update", sid, "--patch", str(patch)).exit_code == 0

    latest = json.loads(_run("get", sid, "--format", "json").stdout)
    assert latest["selected_version"] == 2
    assert [v["version"] for v in latest["versions"]] == [1, 2]

    first = json.loads(_run("get", sid, "--version", "1", "-f", "json").stdout)
    assert first["selected_version"] == 1

    as_yaml = yaml.safe_load(_run("get", sid, "--format", "yaml").stdout)
    assert as_yaml["name"] == "alpha"

    table = _run("get", sid)
    assert table.exit_code == 0
    assert "alpha" in table.stdout
    assert "99" in table.stdout


def test_update_full_replace_and_table_output(tmp_path: Path) -> None:
    sid = _create(tmp_path)["strategy_id"]
    replacement = _write(tmp_path, "new.json", {"window": 50})
    result = _run("update", sid, "--params", str(replacement), "--desc", "nuevo", "--tag", "t")
    assert result.exit_code == 0
    assert "v2" in result.stdout
    strategy = json.loads(_run("get", sid, "-f", "json").stdout)
    assert strategy["description"] == "nuevo"
    assert strategy["tags"] == ["t"]


def test_list_filters_and_formats(tmp_path: Path) -> None:
    _create(tmp_path, "trend_follow", None, "--tag", "fx")
    _create(tmp_path, "mean_rev", {"w": 2})
    everything = json.loads(_run("list", "--format", "json").stdout)
    assert everything["total_count"] == 2
    by_tag = json.loads(_run("list", "--tag", "fx", "-f", "json").stdout)
    assert [i["name"] for i in by_tag["items"]] == ["trend_follow"]
    by_name = json.loads(_run("list", "--name", "REV", "-f", "json").stdout)
    assert [i["name"] for i in by_name["items"]] == ["mean_rev"]
    page = json.loads(_run("list", "--limit", "1", "--offset", "1", "-f", "json").stdout)
    assert len(page["items"]) == 1
    assert page["total_count"] == 2
    table = _run("list")
    assert "trend_follow" in table.stdout
    assert "2 de 2" in table.stdout


def test_diff_accepts_v_prefix_and_all_formats(tmp_path: Path) -> None:
    sid = _create(tmp_path, "alpha", {"a": 1, "gone": 1})["strategy_id"]
    new = _write(tmp_path, "n.json", {"a": 2, "added": True})
    assert _run("update", sid, "--params", str(new)).exit_code == 0

    data = json.loads(_run("diff", sid, "--from", "v1", "--to", "v2", "-f", "json").stdout)
    assert data["added"] == {"added": True}
    assert data["removed"] == {"gone": 1}
    assert data["changed"] == {"a": [1, 2]}
    assert yaml.safe_load(_run("diff", sid, "--from", "1", "--to", "2", "-f", "yaml").stdout)[
        "added"
    ] == {"added": True}
    table = _run("diff", sid, "--from", "1", "--to", "2")
    assert "añadido" in table.stdout
    assert "eliminado" in table.stdout
    assert "cambiado" in table.stdout
    same = _run("diff", sid, "--from", "1", "--to", "1")
    assert "Sin diferencias" in same.stdout


def test_versions_command(tmp_path: Path) -> None:
    sid = _create(tmp_path)["strategy_id"]
    assert _run("update", sid, "-p", str(_write(tmp_path, "n.json", {"w": 1}))).exit_code == 0
    listed = json.loads(_run("versions", sid, "-f", "json").stdout)
    assert [v["version"] for v in listed] == [1, 2]
    page = json.loads(_run("versions", sid, "--limit", "1", "--offset", "1", "-f", "json").stdout)
    assert [v["version"] for v in page] == [2]
    assert "Versiones" in _run("versions", sid).stdout


def test_delete_soft_then_purge_with_confirmation(tmp_path: Path) -> None:
    sid = _create(tmp_path)["strategy_id"]
    assert _run("delete", sid).exit_code == 0
    assert json.loads(_run("list", "-f", "json").stdout)["total_count"] == 0
    hidden = json.loads(_run("list", "--include-deleted", "-f", "json").stdout)
    assert hidden["total_count"] == 1
    assert json.loads(_run("get", sid, "-f", "json").stdout)["deleted_at_ms"] is not None

    declined = _run("delete", sid, "--purge", input_="n\n")
    assert declined.exit_code == 1  # Abort de click
    assert _run("get", sid).exit_code == 0

    accepted = _run("delete", sid, "--purge", input_="y\n")
    assert accepted.exit_code == 0
    assert _run("get", sid).exit_code == cli.EXIT_NOT_FOUND


def test_delete_purge_force_skips_confirmation(tmp_path: Path) -> None:
    sid = _create(tmp_path)["strategy_id"]
    assert _run("delete", sid, "--purge", "--force").exit_code == 0
    assert _run("get", sid).exit_code == cli.EXIT_NOT_FOUND


def test_export_stdout_and_file(tmp_path: Path) -> None:
    sid = _create(tmp_path)["strategy_id"]
    document = json.loads(_run("export", sid).stdout)
    assert document["strategy"]["id"] == sid
    as_yaml = yaml.safe_load(_run("export", sid, "--format", "yaml").stdout)
    assert as_yaml["strategy"]["name"] == "alpha"
    target = tmp_path / "out" / "alpha.json"
    result = _run("export", sid, "--output", str(target))
    assert result.exit_code == 0
    assert json.loads(target.read_text(encoding="utf-8"))["strategy"]["id"] == sid


# -------------------------------------------------------------------------- exit codes
def test_cli_exit_codes_not_found() -> None:
    ghost = "00000000-0000-7000-8000-000000000000"
    for args in (
        ("get", ghost),
        ("diff", ghost, "--from", "1", "--to", "2"),
        ("versions", ghost),
        ("export", ghost),
    ):
        result = _run(*args)
        assert result.exit_code == cli.EXIT_NOT_FOUND, args
        assert "Strategy not found" in result.stderr


def test_cli_exit_codes_update_missing_strategy(tmp_path: Path) -> None:
    params = _write(tmp_path, "p.json", {"a": 1})
    result = _run("update", "ghost", "--params", str(params))
    assert result.exit_code == cli.EXIT_NOT_FOUND


def test_cli_exit_codes_missing_version(tmp_path: Path) -> None:
    sid = _create(tmp_path)["strategy_id"]
    assert _run("get", sid, "--version", "9").exit_code == cli.EXIT_NOT_FOUND
    assert _run("diff", sid, "--from", "1", "--to", "9").exit_code == cli.EXIT_NOT_FOUND


def test_cli_exit_codes_delete_is_idempotent() -> None:
    assert _run("delete", "ghost").exit_code == cli.EXIT_OK
    assert _run("delete", "ghost", "--purge", "--force").exit_code == cli.EXIT_OK


def test_cli_exit_codes_validation(tmp_path: Path) -> None:
    params = _write(tmp_path, "p.json", {"a": 1})
    bad_name = _run("create", "Bad Name", "--params", str(params))
    assert bad_name.exit_code == cli.EXIT_VALIDATION
    assert "name" in bad_name.stderr

    sid = _create(tmp_path)["strategy_id"]
    same = _write(tmp_path, "same.json", {"window": 10, "mode": "a"})
    assert _run("update", sid, "-p", str(same)).exit_code == cli.EXIT_VALIDATION

    bad_patch = _write(tmp_path, "bp.json", [{"op": "remove", "path": "/nope"}])
    assert _run("update", sid, "--patch", str(bad_patch)).exit_code == cli.EXIT_VALIDATION

    both = _run("update", sid, "-p", str(same), "--patch", str(bad_patch))
    assert both.exit_code == cli.EXIT_VALIDATION
    assert "not both" in both.stderr
    neither = _run("update", sid)
    assert neither.exit_code == cli.EXIT_VALIDATION

    assert _run("diff", sid, "--from", "x", "--to", "2").exit_code == cli.EXIT_VALIDATION
    assert _run("diff", sid, "--from", "0", "--to", "2").exit_code == cli.EXIT_VALIDATION


def test_cli_exit_codes_conflict(tmp_path: Path) -> None:
    _create(tmp_path, "alpha")
    params = _write(tmp_path, "p.json", {"a": 1})
    result = _run("create", "alpha", "--params", str(params))
    assert result.exit_code == cli.EXIT_CONFLICT
    assert "already exists" in result.stderr


def test_cli_exit_codes_io_error_on_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    result = _run("create", "alpha", "--params", str(missing))
    assert result.exit_code == cli.EXIT_IO
    assert "Cannot read" in result.stderr
    assert str(missing.name) in result.stderr


def test_cli_exit_codes_io_error_on_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("FT_STRATEGIES_DB_PATH", str(blocker / "s.db"))
    assert _run("list").exit_code == cli.EXIT_IO


# ---------------------------------------------------------------------- input files
def test_input_file_errors_are_clear(tmp_path: Path) -> None:
    broken = _write(tmp_path, "broken.json", "{not json")
    result = _run("create", "alpha", "--params", str(broken))
    assert result.exit_code == cli.EXIT_VALIDATION
    # La terminal parte líneas largas (rutas temporales largas en CI): se compara aplanado.
    assert "not valid JSON" in " ".join(result.stderr.split())

    array = _write(tmp_path, "array.json", [1, 2])
    result = _run("create", "alpha", "--params", str(array))
    assert result.exit_code == cli.EXIT_VALIDATION
    assert "must contain a JSON object" in " ".join(result.stderr.split())

    object_patch = _write(tmp_path, "obj.json", {"op": "add"})
    sid = _create(tmp_path)["strategy_id"]
    result = _run("update", sid, "--patch", str(object_patch))
    assert result.exit_code == cli.EXIT_VALIDATION
    assert "must contain a JSON array" in " ".join(result.stderr.split())

    binary = tmp_path / "bin.json"
    binary.write_bytes(b"\xff\xfe\x00")
    assert _run("create", "beta", "-p", str(binary)).exit_code == cli.EXIT_VALIDATION

    bad_schema = _write(tmp_path, "bs.json", {"type": "bogus"})
    ok_params = _write(tmp_path, "ok.json", {"a": 1})
    result = _run("create", "gamma", "-p", str(ok_params), "-s", str(bad_schema))
    assert result.exit_code == cli.EXIT_VALIDATION
    assert "Invalid parameter_schema" in result.stderr
