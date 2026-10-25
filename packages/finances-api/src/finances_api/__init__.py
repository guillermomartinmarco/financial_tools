"""Raíz de composición HTTP de financial_tools (specs/finances_api)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("finances-api")
except PackageNotFoundError:  # pragma: no cover - ejecución sin instalar
    __version__ = "0.0.0"

__all__ = ["__version__"]
