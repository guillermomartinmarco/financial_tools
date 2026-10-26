"""Tests ``real_data``: leen el almacén real de ``ft_datasets`` (nunca escriben).

El conftest raíz aísla ``FT_PROJECT_ROOT`` en ``tmp_path``; aquí se restaura la raíz real
(la del entorno o la del repositorio) para que ``require_dataset`` lea
``FT_DATASETS_RECIPES_DIR`` / ``FT_DATASETS_DIR`` reales. Sin almacén materializado, los
tests **fallan** con un mensaje claro (nunca se omiten).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
_REAL_ROOT = os.environ.get("FT_PROJECT_ROOT") or str(REPO_ROOT)


@pytest.fixture(autouse=True)
def _real_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_PROJECT_ROOT", _REAL_ROOT)
    if "FT_DATASETS_RECIPES_DIR" not in os.environ:
        monkeypatch.setenv("FT_DATASETS_RECIPES_DIR", str(REPO_ROOT / "datasets"))
