"""RF-19 (ft_ta_lib) / T8.2: guardia de versión de ta-lib.

El catálogo, los tipos de media, el tope de funciones y la semántica de ``RSI`` plano se
validaron contra una versión concreta de ta-lib. Si el entorno resuelve otra, este test falla
con un mensaje accionable en lugar de dejar que fallen varios tests sin relación aparente.
"""

from __future__ import annotations

from typing import Any

import pytest
import talib

from finances_analytics.ft_ta_lib.catalog import c_version

#: Versiones contra las que se validaron specs, catálogo y oráculo (ver spec ft_ta_lib RF-19).
VALIDATED_WRAPPER = "0.8.3"
VALIDATED_C = "0.8.3"


def check_talib_versions(lib: Any) -> None:
    """Falla con un mensaje que nombra la versión instalada, la validada y RF-19."""
    installed = (str(lib.__version__), c_version(lib))
    if installed != (VALIDATED_WRAPPER, VALIDATED_C):
        pytest.fail(
            f"ta-lib instalada {installed[0]} (C {installed[1]}) != validada "
            f"{VALIDATED_WRAPPER} (C {VALIDATED_C}). Subir ta-lib es una decisión explícita: "
            "actualizar primero specs/ft_ta_lib (RF-19) y specs/ft_indicators, revisar catálogo, "
            "MA_TYPE_NAMES, MAX_FUNCTIONS y fixtures del oráculo, y entonces cambiar el pin de "
            "packages/finances-analytics/pyproject.toml y estas constantes.",
            pytrace=False,
        )


def test_installed_talib_is_the_validated_version() -> None:
    check_talib_versions(talib)


class _FakeTalib:
    def __init__(self, version: str, c: bytes) -> None:
        self.__version__ = version
        self.__ta_version__ = c


def test_guard_fails_loudly_with_a_clear_message_on_drift() -> None:
    with pytest.raises(pytest.fail.Exception, match=r"0\.8\.4 \(C 0\.8\.3\).*RF-19"):
        check_talib_versions(_FakeTalib("0.8.4", b"0.8.3 (Oct 28 2026)"))
    with pytest.raises(pytest.fail.Exception, match=r"C 0\.9\.0"):
        check_talib_versions(_FakeTalib("0.8.3", b"0.9.0 (Jan 1 2027)"))
