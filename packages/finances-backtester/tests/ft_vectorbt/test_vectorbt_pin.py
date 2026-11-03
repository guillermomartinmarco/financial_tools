"""V0.2: el pin de ``vectorbt`` es el verificado con evidencia (``specs/ft_vectorbt/tasks.md``).

Cambiar la versión exige repetir la evidencia (``vectorbt-probe``) y actualizar este test.
"""

from __future__ import annotations

import importlib.metadata
import re

VERIFIED_CLAUSES = frozenset({">=1.2.0", "<1.3"})
VERIFIED_PREFIX = "1.2."


def _declared_clauses(name: str) -> frozenset[str]:
    """Cláusulas de versión declaradas (los metadatos no conservan su orden)."""
    requires = importlib.metadata.requires("finances-backtester") or []
    for requirement in requires:
        match = re.fullmatch(rf"{name}\s*([<>=!~][^;]*)", requirement.strip())
        if match:
            return frozenset(match.group(1).replace(" ", "").split(","))
    raise AssertionError(f"finances-backtester no declara {name}: {requires}")


def test_vectorbt_pin_is_the_verified_one() -> None:
    assert _declared_clauses("vectorbt") == VERIFIED_CLAUSES


def test_installed_vectorbt_is_within_the_pin() -> None:
    assert importlib.metadata.version("vectorbt").startswith(VERIFIED_PREFIX)
