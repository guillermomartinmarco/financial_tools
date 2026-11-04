"""B6.3: las señales del núcleo (Polars ``Float32``) igualan bit a bit a ``signals_*.json``.

Se usa el registro de las integradas sin ``FT_STRATEGIES_DIR`` (como el ``runner``) y la
normalización ``finalize`` de RF-16, que es exactamente lo que recibe un motor. Si la
recursión ``Float32`` de Wilder (``rsi_reversion``) divergiera de la referencia en doble
precisión en algún umbral, el primer fallo indica la barra y el RSI de la referencia (A6).
"""

from __future__ import annotations

import polars as pl
import pytest
from finances_core.backtest.catalog import BUILTIN_DIR, build_registry
from finances_core.backtest.strategies.signals import finalize

from tests.contract.fixture import GOLDEN_PARAMS, KINDS, fixture_candles
from tests.contract.golden_io import golden_signals, load_json
from tests.contract.reference import rsi_series


def _core_signals(kind: str, candles: pl.DataFrame) -> tuple[list[bool], list[bool]]:
    strategy = build_registry(builtin_dir=BUILTIN_DIR, local_dir=None).get(kind)
    params = strategy.Params.model_validate(GOLDEN_PARAMS[kind])
    raw = strategy.signals(candles, params)
    sig = finalize(raw, n=candles.height, warmup=strategy.warmup(params))
    assert sig.entries.dtype == pl.Boolean
    assert sig.exits.dtype == pl.Boolean
    return sig.entries.to_list(), sig.exits.to_list()


def _first_difference(got: list[bool], expected: list[bool]) -> int:
    return next(t for t, (a, b) in enumerate(zip(got, expected, strict=True)) if a != b)


@pytest.mark.parametrize("kind", KINDS)
def test_golden_params_are_the_recorded_ones(kind: str) -> None:
    assert load_json(f"signals_{kind}.json")["params"] == GOLDEN_PARAMS[kind]


@pytest.mark.parametrize("kind", KINDS)
def test_core_signals_match_golden_bit_for_bit(kind: str) -> None:
    candles = fixture_candles()
    entries, exits = _core_signals(kind, candles.to_frame())
    golden_entries, golden_exits = golden_signals(kind)
    assert len(entries) == len(golden_entries) == candles.n
    for name, got, expected in (
        ("entries", entries, golden_entries),
        ("exits", exits, golden_exits),
    ):
        if got != expected:
            t = _first_difference(got, expected)
            detail = ""
            if kind == "rsi_reversion":
                rsi = rsi_series(candles.close, int(GOLDEN_PARAMS[kind]["period"]))
                detail = f" (RSI doble t-1={rsi[t - 1]}, t={rsi[t]})"
            pytest.fail(f"{kind}: {name}[{t}] núcleo={got[t]} golden={expected[t]}{detail}")


@pytest.mark.parametrize("kind", KINDS)
def test_core_signals_are_not_trivial(kind: str) -> None:
    # Una igualdad con series vacías no probaría nada: hay entradas y salidas en ambos.
    entries, exits = _core_signals(kind, fixture_candles().to_frame())
    assert sum(entries) >= 5
    assert sum(exits) >= 5
