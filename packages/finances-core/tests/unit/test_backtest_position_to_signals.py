"""G2b: ``signals.position_to_signals`` largo/corto (RF-6, RF-10, Enmienda G2b)."""

from __future__ import annotations

import polars as pl
import pytest

from finances_core.backtest.errors import InvalidSignalsError
from finances_core.backtest.strategies.signals import position_to_signals


def _pos(values: list[int]) -> pl.Series:
    return pl.Series(values, dtype=pl.Int8)


def test_long_only_position_derives_long_flanks_and_empty_short() -> None:
    sig = position_to_signals(_pos([0, 1, 1, 0]))
    assert sig.entries.to_list() == [False, True, False, False]
    assert sig.exits.to_list() == [False, False, False, True]
    assert sig.entries_short.to_list() == [False, False, False, False]
    assert sig.exits_short.to_list() == [False, False, False, False]


def test_short_only_position_derives_short_flanks_and_empty_long() -> None:
    sig = position_to_signals(_pos([0, -1, -1, 0]))
    assert sig.entries.to_list() == [False, False, False, False]
    assert sig.exits.to_list() == [False, False, False, False]
    assert sig.entries_short.to_list() == [False, True, False, False]
    assert sig.exits_short.to_list() == [False, False, False, True]


def test_transition_through_flat_closes_long_and_opens_short() -> None:
    # +1,+1,0,-1,-1: cierra largo en t=2 (prev=1,pos=0) y abre corto en t=3 (prev=0,pos=-1).
    sig = position_to_signals(_pos([1, 1, 0, -1, -1]))
    assert sig.entries.to_list() == [True, False, False, False, False]
    assert sig.exits.to_list() == [False, False, True, False, False]
    assert sig.entries_short.to_list() == [False, False, False, True, False]
    assert sig.exits_short.to_list() == [False, False, False, False, False]


def test_direct_reversal_long_to_short_without_flat_is_rejected() -> None:
    with pytest.raises(InvalidSignalsError, match="invalid_signals") as info:
        position_to_signals(_pos([1, 1, -1]))
    assert "t=2" in str(info.value)


def test_direct_reversal_short_to_long_without_flat_is_rejected() -> None:
    with pytest.raises(InvalidSignalsError, match="invalid_signals") as info:
        position_to_signals(_pos([-1, -1, 1]))
    assert "t=2" in str(info.value)


def test_first_bar_short_is_not_a_reversal() -> None:
    # prev[0] = 0 (fill_null): no hay barra -1 previa, nunca es reversa.
    sig = position_to_signals(_pos([-1, -1, 0]))
    assert sig.entries_short.to_list() == [True, False, False]
    assert sig.exits_short.to_list() == [False, False, True]


def test_all_four_series_are_boolean_and_same_length() -> None:
    sig = position_to_signals(_pos([1, 0, -1, 0]))  # pasa por 0 entre +1 y -1 (válido)
    for series in (sig.entries, sig.exits, sig.entries_short, sig.exits_short):
        assert series.dtype == pl.Boolean
        assert series.len() == 4
