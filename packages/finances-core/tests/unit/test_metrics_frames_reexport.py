"""M0: el contrato canónico vive en `finances_core.metrics.frames` y `backtest.frames`
lo re-exporta sin cambiar su API (decisión (i), una sola fuente de verdad)."""

from __future__ import annotations

from finances_core import metrics
from finances_core.backtest import frames as backtest_frames
from finances_core.metrics import frames as metrics_frames

_CONTRACT = (
    "TRADES_SCHEMA",
    "EQUITY_SCHEMA",
    "SIDE_LONG",
    "SIDE_DOMAIN",
    "EXIT_REASON_SIGNAL",
    "EXIT_REASON_END_OF_DATA",
    "EXIT_REASON_STOP_LOSS",
    "EXIT_REASON_TAKE_PROFIT",
    "EXIT_REASON_DOMAIN",
)


def test_backtest_frames_reexports_the_same_contract_objects() -> None:
    for name in _CONTRACT:
        assert getattr(backtest_frames, name) is getattr(metrics_frames, name), name


def test_metrics_package_exposes_the_contract() -> None:
    for name in _CONTRACT:
        assert getattr(metrics, name) is getattr(metrics_frames, name), name


def test_validate_engine_output_stays_in_backtest_frames() -> None:
    # La validación de salida de motor (lanza OutputOutOfRangeError) es específica del
    # backtest y NO se promueve al hogar neutral.
    assert hasattr(backtest_frames, "validate_engine_output")
    assert not hasattr(metrics_frames, "validate_engine_output")
