"""B5.2 (finances_backtester): ``BACKTEST_MAX_CONCURRENT`` (RF-12) y ``STRATEGIES_DIR`` (A21)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from finances_core.config import FinancesSettings


def test_backtest_max_concurrent_default() -> None:
    assert FinancesSettings().BACKTEST_MAX_CONCURRENT == 2


@pytest.mark.parametrize("value", ["1", "16"])
def test_backtest_max_concurrent_accepts_bounds(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("FT_BACKTEST_MAX_CONCURRENT", value)
    assert int(value) == FinancesSettings().BACKTEST_MAX_CONCURRENT


@pytest.mark.parametrize("value", ["0", "17"])
def test_backtest_max_concurrent_out_of_bounds_fails(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("FT_BACKTEST_MAX_CONCURRENT", value)
    with pytest.raises(ValidationError, match="BACKTEST_MAX_CONCURRENT"):
        FinancesSettings()


def test_strategies_dir_default_resolved_against_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings = FinancesSettings()
    assert settings.STRATEGIES_DIR.is_absolute()
    assert (tmp_path / "strategies").resolve() == settings.STRATEGIES_DIR


def test_relative_strategies_dir_resolves_against_project_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    cwd = tmp_path / "cwd"
    root.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("FT_PROJECT_ROOT", str(root))
    monkeypatch.setenv("FT_STRATEGIES_DIR", "my/strategies")
    monkeypatch.chdir(cwd)
    settings = FinancesSettings()
    assert (root / "my" / "strategies").resolve() == settings.STRATEGIES_DIR
    assert not settings.STRATEGIES_DIR.is_relative_to(cwd.resolve())


def test_absolute_strategies_dir_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = (tmp_path / "abs" / "strategies").resolve()
    monkeypatch.setenv("FT_STRATEGIES_DIR", str(target))
    assert target == FinancesSettings().STRATEGIES_DIR
