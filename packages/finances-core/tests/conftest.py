"""Fixtures compartidas: ningún test escribe en la raíz real del proyecto."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Los tests que necesitan otra raíz la sobrescriben (chdir + delenv o setenv).
    monkeypatch.setenv("FT_PROJECT_ROOT", str(tmp_path))
