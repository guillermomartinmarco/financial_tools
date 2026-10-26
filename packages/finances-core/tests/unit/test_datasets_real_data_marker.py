"""T0.1 de ``ft_datasets``: marcador ``real_data`` excluido por defecto (RF-10).

Comprueba la configuración de pytest de los tres paquetes que alojan tests de datos reales:
el marcador está registrado y ``addopts`` lo excluye (conservando ``not benchmark`` donde
ya existía), de modo que ``pytest -q`` sigue sin red y ``pytest -m real_data`` lo ejecuta.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Any

import pytest

PACKAGES_DIR = Path(__file__).resolve().parents[3]
REAL_DATA_PACKAGES = ("finances-core", "finances-downloader", "finances-analytics")


def _pytest_config(package: str) -> dict[str, Any]:
    pyproject = PACKAGES_DIR / package / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    config: dict[str, Any] = data["tool"]["pytest"]["ini_options"]
    return config


def _marker_expression(addopts: str) -> str:
    args = shlex.split(addopts)
    expressions = [args[i + 1] for i, arg in enumerate(args) if arg == "-m"]
    assert len(expressions) == 1, f"se espera un único -m en addopts: {addopts!r}"
    return expressions[0]


@pytest.mark.parametrize("package", REAL_DATA_PACKAGES)
def test_real_data_marker_registered_and_excluded_by_default(package: str) -> None:
    config = _pytest_config(package)
    markers = [str(m).split(":", 1)[0].strip() for m in config.get("markers", [])]
    assert "real_data" in markers
    expression = _marker_expression(str(config["addopts"]))
    assert "not real_data" in expression


def test_analytics_keeps_benchmark_exclusion() -> None:
    config = _pytest_config("finances-analytics")
    markers = [str(m).split(":", 1)[0].strip() for m in config["markers"]]
    assert "benchmark" in markers
    expression = _marker_expression(str(config["addopts"]))
    assert "not benchmark" in expression
    assert "not real_data" in expression
