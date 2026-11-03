"""B0.3: el paquete se instala, se importa y declara su versión."""

from __future__ import annotations

import importlib.metadata

import finances_backtester


def test_package_version_matches_metadata() -> None:
    assert finances_backtester.__version__ == importlib.metadata.version("finances-backtester")
