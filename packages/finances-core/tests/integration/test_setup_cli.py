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
