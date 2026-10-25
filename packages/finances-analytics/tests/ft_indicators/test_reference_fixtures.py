"""T2.3: *fixtures* publicados / calculados a mano en ``fixtures/reference/``."""

from __future__ import annotations

import re

import pytest

from finances_analytics.ft_indicators.catalog import CATALOG

from .helpers import REFERENCE_DIR

#: Indicador del catálogo → *fixtures* que lo cubren (los tests de kernels los consumen).
COVERAGE = {
    "sma": ["stockcharts_sma_ema_10.csv"],
    "ema": ["stockcharts_sma_ema_10.csv"],
    "wma": ["hand_wma_3.csv"],
    "rsi": ["stockcharts_rsi_14.csv"],
    "macd": ["hand_macd_2_3_2.csv"],
    "bb": ["stockcharts_bb_20_2.csv"],
    "atr": ["stockcharts_atr_14.csv"],
    "stoch": ["stockcharts_stoch_14.csv"],
    "roc": ["stockcharts_roc_12.csv"],
    "obv": ["hand_obv.csv"],
    "adx": ["stockcharts_adx_14.csv"],
    "vwap": ["hand_vwap_3.csv"],
}


def test_every_catalog_indicator_has_a_published_or_hand_fixture() -> None:
    assert set(COVERAGE) == set(CATALOG)
    for name, files in COVERAGE.items():
        assert files, name
        for file in files:
            assert (REFERENCE_DIR / file).is_file(), file


@pytest.mark.parametrize("name", sorted(p.name for p in REFERENCE_DIR.glob("*.csv")))
def test_fixture_header_declares_source_and_tolerance(name: str) -> None:
    text = (REFERENCE_DIR / name).read_text(encoding="utf-8")
    joined = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    assert re.search(r"^# Fuente: ", joined, re.MULTILINE), "sin fuente"
    assert "tolerancia" in joined.lower(), "sin tolerancia de la publicación"
    if name.startswith("stockcharts_"):
        assert "chartschool.stockcharts.com" in joined
        assert re.search(r"sha256 [0-9a-f]{64}", joined)
