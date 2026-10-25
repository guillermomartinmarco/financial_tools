"""Tests de integración del CLI ``ft_setup`` (Art. 7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from finances_core.schemas.setup import (
    EnvVarDiagnosis,
    EnvVarSpec,
    SetupDoctorResponse,
    VarSource,
)
from finances_core.tasks import SQLTaskManager
from ft_setup import cli

runner = CliRunner()


def _response_with_missing_required() -> SetupDoctorResponse:
    spec = EnvVarSpec(
        name="FT_REQUIRED_THING", description="requerida", required=True, var_type="str"
    )
    diagnosis = EnvVarDiagnosis(
        spec=spec,
        masked_value="<unset>",
        source=VarSource.MISSING,
        valid=False,
        validation_error="variable requerida ausente",
    )
    return SetupDoctorResponse(
        ok=False,
        timestamp_ms=0,
        vars=[diagnosis],
        summary={
            "total": 1,
            "required_ok": 0,
            "required_missing": 1,
            "optional_set": 0,
            "optional_unset": 0,
            "invalid": 1,
        },
    )


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output
    assert "doctor" in result.output


def test_doctor_json_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["--quiet", "doctor", "--format", "json"])
    assert result.exit_code == cli.EXIT_OK, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["summary"]["required_missing"] == 0
    names = {item["spec"]["name"] for item in payload["vars"]}
    assert {"FT_DATA_DIR", "FT_API_PORT", "FT_DOWNLOAD_MAX_CONCURRENT"} <= names


def test_doctor_json_masks_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FT_BINANCE_API_KEY", "clave-real")
    result = runner.invoke(cli.app, ["--quiet", "doctor", "--format", "json"])
    assert "clave-real" not in result.output
    payload = json.loads(result.output)
    api_key = next(v for v in payload["vars"] if v["spec"]["name"] == "FT_BINANCE_API_KEY")
    assert api_key["masked_value"] == "***"
    assert api_key["value"] is None


def test_doctor_invalid_value_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FT_API_PORT", "70000")
    result = runner.invoke(cli.app, ["--quiet", "doctor", "--format", "table"])
    assert result.exit_code == cli.EXIT_INVALID


def test_doctor_missing_required_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "diagnose", _response_with_missing_required)
    result = runner.invoke(cli.app, ["--quiet", "doctor", "--format", "table"])
    assert result.exit_code == cli.EXIT_REQUIRED_MISSING


def test_init_creates_files_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(cli.app, ["--quiet", "init"])
    assert first.exit_code == cli.EXIT_OK, first.output
    assert (tmp_path / ".env.template").is_file()
    assert (tmp_path / "share" / "tasks.db").is_file()
    assert (tmp_path / "data" / "parquet").is_dir()

    second = runner.invoke(cli.app, ["--quiet", "init"])
    assert second.exit_code == cli.EXIT_OK
    assert "Archivos generados" in second.output


def test_init_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["--quiet", "init", "--dry-run"])
    assert result.exit_code == cli.EXIT_OK
    assert not (tmp_path / ".env.template").exists()
    assert not (tmp_path / "data").exists()


def test_help_lists_status_command() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert "status" in result.output


def test_init_creates_root_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["--quiet", "init"])
    assert result.exit_code == cli.EXIT_OK, result.output
    assert (tmp_path / ".ft_root").is_file()


def test_init_force_regenerates_env_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli.app, ["--quiet", "init"])
    template = tmp_path / ".env.template"
    template.write_text("editado a mano", encoding="utf-8")

    runner.invoke(cli.app, ["--quiet", "init"])
    assert template.read_text(encoding="utf-8") == "editado a mano"

    forced = runner.invoke(cli.app, ["--quiet", "init", "--force"])
    assert forced.exit_code == cli.EXIT_OK
    assert "FT_DATA_DIR" in template.read_text(encoding="utf-8")


def test_init_io_error_exits_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    def _boom(**_kwargs: object) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(cli, "run_init", _boom)
    result = runner.invoke(cli.app, ["--quiet", "init"])
    assert result.exit_code == cli.EXIT_IO
    assert "disco lleno" in result.output


def test_doctor_invalid_format_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["--quiet", "doctor", "--format", "xml"])
    assert result.exit_code == cli.EXIT_INVALID


def test_global_log_format_json_is_kept_by_doctor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "configure_logging", lambda *args, **kwargs: calls.append((*args, kwargs))
    )
    result = runner.invoke(cli.app, ["--log-format", "json", "doctor", "-v", "--format", "json"])
    assert result.exit_code == cli.EXIT_OK, result.output
    assert calls, "configure_logging no fue llamado"
    for call in calls:
        assert call[-1].get("log_format") == "json"


def test_verbose_flag_sets_debug_level(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    levels: list[object] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_logging", lambda _mode, level, **_kw: levels.append(level))
    runner.invoke(cli.app, ["--verbose", "doctor", "--format", "json"])
    assert levels[0] == 10  # logging.DEBUG


def test_status_reports_init_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli.app, ["--quiet", "init"])
    with SQLTaskManager()._connect() as conn:
        task_id = conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()[0]

    result = runner.invoke(cli.app, ["--quiet", "status", task_id])
    assert result.exit_code == cli.EXIT_OK, result.output
    payload = json.loads(result.output)
    assert payload["task_id"] == task_id
    assert payload["status"] == "COMPLETED"


def test_status_unknown_task_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["--quiet", "status", "00000000-0000-0000-0000-000000000000"])
    assert result.exit_code == cli.EXIT_REQUIRED_MISSING


@pytest.mark.parametrize(
    ("source_tool", "expected"),
    [
        (
            "ft_market_data_binance",
            {
                "FT_BINANCE_MAX_RETRIES",
                "FT_BINANCE_REST_RATE_CAPACITY",
                "FT_DOWNLOAD_MAX_CONCURRENT",
            },
        ),
        ("ft_data_viewer", {"FT_DATA_VIEWER_SAMPLE_MAX_ROWS", "FT_DATA_VIEWER_GAP_THRESHOLD_MS"}),
    ],
)
def test_doctor_lists_tool_variables_with_source_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_tool: str, expected: set[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["--quiet", "doctor", "--format", "json"])
    payload = json.loads(result.output)
    by_tool = {
        v["spec"]["name"] for v in payload["vars"] if v["spec"]["source_tool"] == source_tool
    }
    assert expected <= by_tool


def test_init_env_template_includes_all_tool_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli.app, ["--quiet", "init"])
    template = (tmp_path / ".env.template").read_text(encoding="utf-8")
    for name in ("FT_BINANCE_MAX_RETRIES", "FT_DATA_VIEWER_SAMPLE_MAX_ROWS", "FT_PROJECT_ROOT"):
        assert name in template
