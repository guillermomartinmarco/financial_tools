"""Tests de resolución de raíz del proyecto y rutas absolutas (Art. 2, ft_setup T7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from finances_core.config import ROOT_MARKER, FinancesSettings, find_project_root
from finances_core.tasks import SQLTaskManager


@pytest.fixture(autouse=True)
def _no_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FT_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("FT_TASKS_DB_PATH", raising=False)


def test_find_project_root_walks_up_to_marker(tmp_path: Path) -> None:
    (tmp_path / ROOT_MARKER).touch()
    nested = tmp_path / "packages" / "pkg" / "src"
    nested.mkdir(parents=True)

    assert find_project_root(nested) == tmp_path.resolve()


def test_find_project_root_falls_back_to_start_without_marker(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) == tmp_path.resolve()


def test_find_project_root_env_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ROOT_MARKER).touch()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("FT_PROJECT_ROOT", str(other))

    assert find_project_root(tmp_path) == other.resolve()


def test_tasks_db_is_absolute_under_root_from_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ROOT_MARKER).touch()
    sub = tmp_path / "packages" / "finances-core"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    settings = FinancesSettings()

    assert tmp_path.resolve() / "share" / "tasks.db" == settings.TASKS_DB_PATH
    assert tmp_path.resolve() / "data" / "parquet" == settings.PARQUET_DIR


def test_absolute_env_path_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "custom.db"
    monkeypatch.setenv("FT_TASKS_DB_PATH", str(custom))

    assert custom == FinancesSettings().TASKS_DB_PATH


def test_env_file_is_read_from_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ROOT_MARKER).touch()
    (tmp_path / ".env").write_text("FT_API_PORT=9123\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.delenv("FT_API_PORT", raising=False)

    assert FinancesSettings().API_PORT == 9123


def test_task_manager_default_db_follows_current_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ROOT_MARKER).touch()
    sub = tmp_path / "packages"
    sub.mkdir()
    monkeypatch.chdir(sub)

    manager = SQLTaskManager()

    assert manager.db_path == tmp_path.resolve() / "share" / "tasks.db"
    assert manager.db_path.exists()
