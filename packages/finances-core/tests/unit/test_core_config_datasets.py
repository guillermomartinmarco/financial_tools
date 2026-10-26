"""T1.3 (ft_datasets): settings ``DATASETS_*`` y validación RF-11 de ``require_datasets_dir``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from finances_core.config import ConfigError, FinancesSettings


def test_defaults_resolved_from_project_root(tmp_path: Path) -> None:
    settings = FinancesSettings()
    assert (tmp_path / "datasets").resolve() == settings.DATASETS_RECIPES_DIR
    assert (tmp_path / "data" / "datasets").resolve() == settings.DATASETS_DIR
    assert settings.DATASETS_DOWNLOAD_TIMEOUT_S == 3600
    assert settings.DATASETS_SETTLE_DAYS == 7
    assert settings.require_datasets_dir() == settings.DATASETS_DIR


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_DATASETS_DIR", "cache/ds")
    monkeypatch.setenv("FT_DATASETS_RECIPES_DIR", "recipes")
    monkeypatch.setenv("FT_DATASETS_DOWNLOAD_TIMEOUT_S", "120")
    monkeypatch.setenv("FT_DATASETS_SETTLE_DAYS", "3")
    settings = FinancesSettings()
    assert (tmp_path / "cache" / "ds").resolve() == settings.DATASETS_DIR
    assert (tmp_path / "recipes").resolve() == settings.DATASETS_RECIPES_DIR
    assert settings.DATASETS_DOWNLOAD_TIMEOUT_S == 120
    assert settings.DATASETS_SETTLE_DAYS == 3


def test_absolute_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere" / "ds"
    monkeypatch.setenv("FT_DATASETS_DIR", str(target))
    assert FinancesSettings().require_datasets_dir() == target


@pytest.mark.parametrize(
    ("name", "value"),
    [("FT_DATASETS_DOWNLOAD_TIMEOUT_S", "59"), ("FT_DATASETS_SETTLE_DAYS", "0")],
)
def test_bounds(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        FinancesSettings()


@pytest.mark.parametrize(
    ("datasets_dir", "parquet_dir"),
    [
        ("data/parquet/datasets", "data/parquet"),  # dentro de PARQUET_DIR
        ("data/parquet", "data/parquet"),  # el mismo directorio
        ("data", "data/parquet"),  # lo contiene
    ],
)
def test_datasets_dir_nested_with_parquet_dir_is_config_error(
    monkeypatch: pytest.MonkeyPatch, datasets_dir: str, parquet_dir: str
) -> None:
    monkeypatch.setenv("FT_DATASETS_DIR", datasets_dir)
    monkeypatch.setenv("FT_PARQUET_DIR", parquet_dir)
    settings = FinancesSettings()  # el resto de herramientas no se ve afectado
    with pytest.raises(ConfigError, match="DATASETS_DIR"):
        settings.require_datasets_dir()


def test_sibling_of_parquet_dir_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FT_DATASETS_DIR", "data/parquet_datasets")
    assert (
        FinancesSettings().require_datasets_dir()
        == (tmp_path / "data" / "parquet_datasets").resolve()
    )
