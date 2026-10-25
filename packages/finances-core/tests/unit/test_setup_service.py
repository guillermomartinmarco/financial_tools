"""Tests unitarios de la lógica de ``ft_setup`` (Art. 7: camino feliz, bordes, fallo)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finances_core.schemas.setup import EnvVarSpec, VarSource
from finances_core.tasks import SQLTaskManager
from ft_setup import service


def _spec(**overrides: object) -> EnvVarSpec:
    base: dict[str, object] = {
        "name": "FT_TEST_VALUE",
        "description": "variable de prueba",
        "required": False,
        "var_type": "str",
        "secret": False,
    }
    base.update(overrides)
    return EnvVarSpec.model_validate(base)


# ------------------------------------------------------------------ detect_secret
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("FT_API_KEY", True),
        ("FT_BINANCE_API_SECRET", True),
        ("FT_DB_PASSWORD", True),
        ("FT_ACCESS_TOKEN", True),
        ("FT_PRIVATE_MNEMONIC", True),
        ("FT_DATA_DIR", False),
        ("FT_API_HOST", False),
    ],
)
def test_detect_secret_by_name(name: str, expected: bool) -> None:
    assert service.detect_secret(name) is expected


def test_detect_secret_explicit_flag_wins() -> None:
    assert service.detect_secret("FT_DATA_DIR", spec_secret=True) is True


# -------------------------------------------------------------------- validate_var
@pytest.mark.parametrize(
    ("spec", "value", "expected_valid"),
    [
        (_spec(name="FT_API_PORT", var_type="host_port"), "8000", True),
        (_spec(name="FT_API_PORT", var_type="host_port"), "70000", False),
        (_spec(name="FT_API_PORT", var_type="host_port"), "abc", False),
        (_spec(name="FT_API_HOST", var_type="host_port"), "0.0.0.0", True),
        (_spec(name="FT_API_HOST", var_type="host_port"), "bad host!", False),
        (_spec(name="FT_API_WORKERS", var_type="int"), "4", True),
        (_spec(name="FT_API_WORKERS", var_type="int"), "many", False),
        (_spec(name="FT_RATE", var_type="float"), "10.5", True),
        (_spec(name="FT_RATE", var_type="float"), "nope", False),
        (_spec(name="FT_FLAG", var_type="bool"), "true", True),
        (_spec(name="FT_FLAG", var_type="bool"), "maybe", False),
        (_spec(name="FT_URL", var_type="url"), "https://data.binance.vision", True),
        (_spec(name="FT_URL", var_type="url"), "not-a-url", False),
        (_spec(name="FT_LOG_LEVEL"), "INFO", True),
    ],
)
def test_validate_var_types(spec: EnvVarSpec, value: str, expected_valid: bool) -> None:
    valid, error = service.validate_var(spec, value)
    assert valid is expected_valid
    assert (error is None) is expected_valid


def test_validate_var_path_existing_and_creatable(tmp_path: Path) -> None:
    spec = _spec(name="FT_DATA_DIR", var_type="path")
    assert service.validate_var(spec, str(tmp_path))[0] is True
    assert service.validate_var(spec, str(tmp_path / "nuevo" / "sub"))[0] is True


def test_validate_var_required_missing() -> None:
    spec = _spec(name="FT_DATA_DIR", var_type="path", required=True)
    valid, error = service.validate_var(spec, None)
    assert valid is False
    assert "requerida" in (error or "")


def test_validate_optional_missing() -> None:
    assert service.validate_var(_spec(), None) == (True, None)


# ------------------------------------------------------------------------ diagnose
def test_diagnose_masks_secrets_and_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = [
        _spec(name="FT_BINANCE_API_KEY", secret=True, required=False),
        _spec(name="FT_DATA_DIR", var_type="path", required=True),
    ]
    monkeypatch.setenv("FT_BINANCE_API_KEY", "super-secret")
    monkeypatch.delenv("FT_DATA_DIR", raising=False)

    response = service.diagnose(specs=specs, settings=None, env_file=Path("no-existe.env"))

    by_name = {item.spec.name: item for item in response.vars}
    api_key = by_name["FT_BINANCE_API_KEY"]
    assert api_key.masked_value == "***"
    assert api_key.value is None
    assert "super-secret" not in response.model_dump_json()

    data_dir = by_name["FT_DATA_DIR"]
    assert data_dir.source is VarSource.DEFAULT
    assert response.ok is True

    summary = response.summary
    assert summary["total"] == 2
    assert summary["required_ok"] == 1


def test_diagnose_required_missing_breaks_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = [_spec(name="FT_REQUIRED_THING", required=True)]
    monkeypatch.delenv("FT_REQUIRED_THING", raising=False)
    response = service.diagnose(specs=specs, env_file=Path("no-existe.env"))
    assert response.ok is False
    assert service.has_missing_required(response) is True


def test_diagnose_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = [_spec(name="FT_API_PORT", var_type="host_port", required=True)]
    monkeypatch.setenv("FT_API_PORT", "70000")
    response = service.diagnose(specs=specs, env_file=Path("no-existe.env"))
    assert response.ok is False
    assert response.summary["invalid"] == 1
    assert service.has_missing_required(response) is False


def test_diagnose_reads_dotenv_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('FT_TEST_VALUE="desde-dotenv"\n', encoding="utf-8")
    monkeypatch.delenv("FT_TEST_VALUE", raising=False)
    response = service.diagnose(specs=[_spec()], env_file=env_file)
    assert response.vars[0].source is VarSource.DOTENV


# --------------------------------------------------------------------------- init
def test_run_init_creates_structure_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for var in (
        "FT_DATA_DIR",
        "FT_RAW_DIR",
        "FT_PROCESSED_DIR",
        "FT_PARQUET_DIR",
        "FT_LOGS_DIR",
        "FT_STRATEGIES_DIR",
        "FT_RESULTS_DIR",
        "FT_SHARE_DIR",
        "FT_TASKS_DB_PATH",
    ):
        monkeypatch.delenv(var, raising=False)

    manager = SQLTaskManager(tmp_path / "share" / "tasks.db")
    first = service.run_init(project_root=tmp_path, manager=manager)

    assert first.status == "COMPLETED"
    expected_dirs = (
        "data/raw",
        "data/processed",
        "data/parquet",
        "logs",
        "strategies",
        "results",
        "share",
    )
    for directory in expected_dirs:
        assert (tmp_path / directory).is_dir()
    assert (tmp_path / ".env.template").is_file()
    assert (tmp_path / ".env.template").read_text(encoding="utf-8").count("FT_DATA_DIR") == 1

    record = manager.get_task(first.task_id)
    assert record is not None
    assert record["status"] == "COMPLETED"
    summary = json.loads(record["result_summary_json"])
    assert summary["ok"] is True

    second = service.run_init(project_root=tmp_path, manager=manager)
    assert second.generated_files == []
    assert manager.get_task(second.task_id)["status"] == "COMPLETED"


def test_run_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    manager = SQLTaskManager(tmp_path / "share" / "tasks.db")
    result = service.run_init(force=True, dry_run=True, project_root=tmp_path, manager=manager)

    assert result.status == "COMPLETED"
    assert result.generated_files  # se planificaron
    assert not (tmp_path / ".env.template").exists()
    assert not (tmp_path / "data").exists()


def test_run_init_persists_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager = SQLTaskManager(tmp_path / "share" / "tasks.db")

    def _boom(*_args: object, **_kwargs: object) -> list[EnvVarSpec]:
        raise RuntimeError("env_list corrupto")

    monkeypatch.setattr(service, "load_env_specs", _boom)

    with pytest.raises(RuntimeError):
        service.run_init(project_root=tmp_path, manager=manager)

    tasks = manager.list_tasks()
    assert tasks[0]["status"] == "FAILED"
    assert "env_list corrupto" in (tasks[0]["error"] or "")


def test_get_init_result_missing_returns_none(tmp_path: Path) -> None:
    manager = SQLTaskManager(tmp_path / "tasks.db")
    assert service.get_init_result("00000000-0000-0000-0000-000000000000", manager) is None
