"""Configuración compartida de los tests de finances-api."""

from __future__ import annotations

import os

# Typer fuerza salida de terminal (códigos ANSI) si detecta GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS;
# se desactiva antes de importar Typer para que los tests vean texto plano en cualquier entorno.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
