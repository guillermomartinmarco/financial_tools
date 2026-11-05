"""V1.5: precisión de ``VectorbtEngine`` frente al resultado canónico (RFC-0004 punto 5).

Suite de contrato de B6.2 sin modificar: las 3 estrategias integradas contra los *golden*
versionados y la estrategia sintética (RF-21) contra la referencia independiente, con las
tolerancias de ``engine_contract.TOLERANCES`` (exactos: ``n_trades``, tiempos, ``side``,
``exit_reason``; precios 1e-6 relativo; dinero 1e-4 relativo con mínimo absoluto;
``drawdown`` 1e-5 absoluto; métricas del resumen 1e-3 relativo).
"""

from __future__ import annotations

import pytest

from finances_backtester.ft_vectorbt.engine import VectorbtEngine
from tests.contract.engine_contract import CONTRACT_KINDS, assert_engine_matches_golden


@pytest.mark.parametrize("kind", CONTRACT_KINDS)
def test_engine_matches_golden_within_rfc_0004_tolerances(kind: str) -> None:
    assert_engine_matches_golden(VectorbtEngine(), kind)
