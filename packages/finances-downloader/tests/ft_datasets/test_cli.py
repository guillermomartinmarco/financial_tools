"""T4.1 — CLI ``ft_datasets``: un test por comando y por código de salida (0-6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from finances_core.config import FinancesSettings
from finances_core.datasets import recipes
from typer.testing import CliRunner

from finances_downloader.ft_datasets import cli, service

from .conftest import AAPL_KEY, ALL_KEYS, RECIPE_NAME, FakeRunner, write_item

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake_runner = FakeRunner()
    monkeypatch.setattr(cli, "make_runner", lambda: fake_runner)
    return fake_runner


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(cli.app, ["--quiet", *args])
    return result.exit_code, result.stdout


def _materialize_and_lock(fake: FakeRunner) -> None:
    assert _invoke("materialize", RECIPE_NAME)[0] == 6
    assert _invoke("lock", RECIPE_NAME)[0] == 0


def test_list_exit_0(recipe_dir: Path) -> None:
    code, out = _invoke("list")
    assert code == 0
    assert [(r["name"], r["lock"], r["items"]) for r in json.loads(out)] == [
        (RECIPE_NAME, "UNLOCKED", 3)
    ]


def test_show_exit_0_and_not_found_exit_1(recipe_dir: Path) -> None:
    code, out = _invoke("show", RECIPE_NAME)
    assert code == 0
    assert [i["key"] for i in json.loads(out)["items"]] == ALL_KEYS
    assert _invoke("show", "nope")[0] == 1


def test_invalid_name_or_recipe_exit_2(recipe_dir: Path) -> None:
    assert _invoke("show", "../x")[0] == 2
    (recipe_dir / "bad.toml").write_text("schema_version = 1\nname = 'bad'\n", encoding="utf-8")
    assert _invoke("verify", "bad")[0] == 2
    assert _invoke("materialize", "bad")[0] == 2


def test_corrupt_lock_exit_2(recipe_dir: Path) -> None:
    (recipe_dir / f"{RECIPE_NAME}.lock.json").write_text('{"hash_algorithm": "md5"}')
    assert _invoke("verify", RECIPE_NAME)[0] == 2


def test_materialize_unlocked_exit_6_then_lock_verify_ok(
    recipe_dir: Path, fake: FakeRunner
) -> None:
    code, out = _invoke("materialize", RECIPE_NAME)
    assert code == 6
    payload = json.loads(out)
    assert payload["status"] == "COMPLETED"
    assert payload["outcome"] == "UNLOCKED"
    assert len(fake.calls) == 2

    code, out = _invoke("lock", RECIPE_NAME)
    assert code == 0
    assert json.loads(out)["outcome"] == "WRITTEN"
    assert json.loads(_invoke("lock", RECIPE_NAME)[1])["outcome"] == "UNCHANGED"

    code, out = _invoke("verify", RECIPE_NAME)
    assert code == 0
    assert json.loads(out)["status"] == "OK"

    code, out = _invoke("materialize", RECIPE_NAME)
    assert code == 0
    assert json.loads(out)["blocks"] == []
    assert len(fake.calls) == 2


def test_materialize_failed_exit_3(recipe_dir: Path, fake: FakeRunner) -> None:
    fake.exit_code = 1
    code, out = _invoke("materialize", RECIPE_NAME)
    assert code == 3
    assert json.loads(out)["outcome"] == "DOWNLOAD_FAILED"


def test_materialize_no_verify_exit_0_and_dry_run(recipe_dir: Path, fake: FakeRunner) -> None:
    code, out = _invoke("materialize", RECIPE_NAME, "--dry-run")
    assert code == 0
    assert len(json.loads(out)["blocks"]) == 2
    assert fake.calls == []
    code, out = _invoke("materialize", RECIPE_NAME, "--no-verify")
    assert code == 0
    assert json.loads(out)["outcome"] == "NOT_VERIFIED"


def test_io_error_exit_4(recipe_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_name: str) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(service, "verify", boom)
    assert _invoke("verify", RECIPE_NAME)[0] == 4


def test_verify_mismatch_markdown_and_lock_rejected_exit_5(
    recipe_dir: Path, fake: FakeRunner
) -> None:
    _materialize_and_lock(fake)
    settings = FinancesSettings()
    recipe = recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    aapl = next(i for i in recipe.items() if i.key == AAPL_KEY)
    write_item(settings.DATASETS_DIR, aapl, scale=0.25)

    code, out = _invoke("verify", RECIPE_NAME, "--format", "markdown")
    assert code == 5
    assert "SCALE_RESTATED" in out
    assert "| `yfinance/equity/AAPL/1d@2024-01-01..2024-02-01` | MISMATCH |" in out

    code, out = _invoke("lock", RECIPE_NAME)
    assert code == 5
    assert json.loads(out)["outcome"] == "REJECTED"
    code, out = _invoke("lock", RECIPE_NAME, "--update")
    assert code == 0
    assert json.loads(out)["outcome"] == "UPDATED"


def test_non_canonical_data_exit_5(recipe_dir: Path, fake: FakeRunner) -> None:
    _materialize_and_lock(fake)
    settings = FinancesSettings()
    for path in (settings.DATASETS_DIR / "yfinance").rglob("*.parquet"):
        path.write_bytes(b"garbage")
    assert _invoke("verify", RECIPE_NAME)[0] == 5
    assert _invoke("lock", RECIPE_NAME)[0] == 5


def test_lock_missing_exit_5(recipe_dir: Path) -> None:
    code, out = _invoke("lock", RECIPE_NAME)
    assert code == 5
    assert json.loads(out)["outcome"] == "REJECTED"


def test_status_plan_result_exit_0_and_unknown_exit_1(recipe_dir: Path, fake: FakeRunner) -> None:
    _invoke("materialize", RECIPE_NAME)
    settings = FinancesSettings()
    plans = list((settings.SHARE_DIR / "plans").glob("datasets_plan_*.json"))
    assert len(plans) == 1
    task_id = plans[0].stem.removeprefix("datasets_plan_")
    for command in ("status", "plan", "result"):
        code, out = _invoke(command, task_id)
        assert code == 0, command
        assert json.loads(out)["task_id"] == task_id
        assert _invoke(command, "0192f000-0000-7000-8000-0000000000ff")[0] == 1


def test_result_of_unfinished_task_exit_1(recipe_dir: Path) -> None:
    task_id = "0192f000-0000-7000-8000-0000000000aa"
    service.create_materialize_task(task_id, RECIPE_NAME)
    assert _invoke("result", task_id)[0] == 1


def test_global_flags_accepted(recipe_dir: Path) -> None:
    result = runner.invoke(cli.app, ["-v", "--log-format", "json", "list"])
    assert result.exit_code == 0
