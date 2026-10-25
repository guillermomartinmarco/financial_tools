"""Fixtures compartidas: ningún test escribe en la raíz real del proyecto."""

from __future__ import annotations

import os

# Typer fuerza salida de terminal (códigos ANSI) si detecta GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS;
# se desactiva antes de importar Typer para que los tests vean texto plano en cualquier entorno.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Toda ruta de ``FinancesSettings`` (PARQUET_DIR, TASKS_DB_PATH, ...) cae en ``tmp_path``."""
    monkeypatch.setenv("FT_PROJECT_ROOT", str(tmp_path))
