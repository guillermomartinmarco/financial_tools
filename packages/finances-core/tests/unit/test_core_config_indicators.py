"""T1.12 (ft_indicators): ``INDICATORS_DIR``/``INDICATORS_MAX_CONCURRENT`` y validación RF-7."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from finances_core.config import ConfigError, FinancesSettings


def test_defaults_resolved_from_project_root(tmp_path: Path) -> None:
    settings = FinancesSettings()
    assert (tmp_path / "data" / "indicators").resolve() == settings.INDICATORS_DIR
    assert settings.INDICATORS_MAX_CONCURRENT == 2
    assert settings.require_indicators_dir() == settings.INDICATORS_DIR


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_INDICATORS_DIR", "custom/ind")
    monkeypatch.setenv("FT_INDICATORS_MAX_CONCURRENT", "5")
    settings = FinancesSettings()
    assert (tmp_path / "custom" / "ind").resolve() == settings.INDICATORS_DIR
    assert settings.INDICATORS_MAX_CONCURRENT == 5


def test_absolute_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere" / "indicators"
    monkeypatch.setenv("FT_INDICATORS_DIR", str(target))
    assert target == FinancesSettings().INDICATORS_DIR


@pytest.mark.parametrize("value", ["0", "17"])
def test_max_concurrent_bounds(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FT_INDICATORS_MAX_CONCURRENT", value)
    with pytest.raises(ValidationError):
        FinancesSettings()


@pytest.mark.parametrize("inside", ["data/parquet/indicators", "data/parquet"])
def test_indicators_dir_inside_parquet_dir_is_config_error(
    monkeypatch: pytest.MonkeyPatch, inside: str
) -> None:
    monkeypatch.setenv("FT_INDICATORS_DIR", inside)
    settings = FinancesSettings()  # el resto de herramientas no se ve afectado
    with pytest.raises(ConfigError, match="INDICATORS_DIR"):
        settings.require_indicators_dir()


def test_sibling_of_parquet_dir_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FT_INDICATORS_DIR", "data/parquet_indicators")
    assert (
        FinancesSettings().require_indicators_dir()
        == (tmp_path / "data" / "parquet_indicators").resolve()
    )
