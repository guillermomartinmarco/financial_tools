"""G3: ``signals.position_from_long_short`` (RF-2, Enmienda G3): máquina de estados 3-way."""

from __future__ import annotations

import itertools

import polars as pl

from finances_core.backtest.strategies.signals import position_from_long_short, position_to_signals


def _series(values: list[bool]) -> pl.Series:
    return pl.Series(values, dtype=pl.Boolean)


def _false(n: int) -> pl.Series:
    return _series([False] * n)


def test_degenerate_case_single_pulse_event_still_reverses_to_short() -> None:
    # Caso familia direccional: exit_long = enter_short (el mismo pulso de un bar); el
    # pulso ya no está activo en t+1, pero la reversa se aplica igual (memoria 'pending').
    bullish = _series([True, False, False, False, False, False, True, False, False, False, False])
    bearish = _series([False, False, False, False, False, True, False, False, False, False, True])
    pos = position_from_long_short(
        enter_long=bullish, exit_long=bearish, enter_short=bearish, exit_short=bullish
    )
    # t5: bearish mientras largo -> plano + pending=-1. t6: pending se aplica -> corto,
    # aunque bullish[6]=True (el pulso de entrada ya no importa, la reversa ya quedó
    # decidida). t10: bearish de nuevo, pero ya estábamos cortos (ni exit_short=bullish[10]
    # ni enter_long=bullish[10] son ciertos) -> se sostiene corto.
    assert pos.to_list() == [1, 1, 1, 1, 1, 0, -1, -1, -1, -1, -1]


def test_never_produces_a_direct_reversal_by_construction() -> None:
    # Pulsos alternados agresivos: nunca debe aparecer +1 seguido de -1 (o viceversa).
    n = 20
    bullish = _series([i % 4 == 0 for i in range(n)])
    bearish = _series([i % 4 == 2 for i in range(n)])
    pos = position_from_long_short(
        enter_long=bullish, exit_long=bearish, enter_short=bearish, exit_short=bullish
    ).to_list()
    for prev, cur in zip(pos, pos[1:], strict=False):
        assert not (prev == 1 and cur == -1)
        assert not (prev == -1 and cur == 1)


def test_distinct_four_conditions_family_exit_never_closes_the_wrong_side() -> None:
    # Familia reversión: 4 condiciones distintas. Una condición de salida de largo que
    # también sería cierta estando en corto NO debe cerrar el corto (el bug que motivó la
    # máquina de estados: exit_long solo se evalúa mientras s==1).
    # t0: enter_short (abre corto). t1: exit_long=True (irrelevante, no estamos largos) ->
    # debe seguir en corto. t2: exit_short=True -> recién ahí cierra a plano.
    enter_long = _false(3)
    exit_long = _series([False, True, False])  # True en t1, mientras estamos CORTOS
    enter_short = _series([True, False, False])
    exit_short = _series([False, False, True])
    pos = position_from_long_short(enter_long, exit_long, enter_short, exit_short)
    assert pos.to_list() == [-1, -1, 0]


def test_flat_state_prefers_long_on_simultaneous_entries() -> None:
    pos = position_from_long_short(
        enter_long=_series([True]),
        exit_long=_false(1),
        enter_short=_series([True]),
        exit_short=_false(1),
    )
    assert pos.to_list() == [1]


def test_all_false_stays_flat() -> None:
    pos = position_from_long_short(_false(5), _false(5), _false(5), _false(5))
    assert pos.to_list() == [0] * 5


def test_output_is_int8() -> None:
    pos = position_from_long_short(_false(3), _false(3), _false(3), _false(3))
    assert pos.dtype == pl.Int8


def test_prefix_stability() -> None:
    bullish = _series([i % 5 == 0 for i in range(30)])
    bearish = _series([i % 5 == 2 for i in range(30)])
    full = position_from_long_short(
        enter_long=bullish, exit_long=bearish, enter_short=bearish, exit_short=bullish
    ).to_list()
    for k in (5, 11, 17, 29, 30):
        part = position_from_long_short(
            enter_long=bullish.head(k),
            exit_long=bearish.head(k),
            enter_short=bearish.head(k),
            exit_short=bullish.head(k),
        ).to_list()
        assert part == full[:k], f"prefijo inestable en k={k}"


def test_nulls_in_inputs_are_treated_as_false() -> None:
    enter_long = pl.Series([True, None, False], dtype=pl.Boolean)
    pos = position_from_long_short(enter_long, _false(3), _false(3), _false(3))
    assert pos.to_list() == [1, 1, 1]  # el null no dispara nada; se sostiene el largo


def test_simultaneous_entries_while_long_queue_short_reversal() -> None:
    # enter_long y enter_short ciertos la misma barra, estando largo: gana enter_short
    # (prioridad de reversa sobre sostener), nunca una reversa directa.
    el = _series([True, True])
    es = _series([False, True])
    pos = position_from_long_short(el, _false(2), es, _false(2))
    assert pos.to_list() == [1, 0]  # t1: pasa a plano con pending=-1 (se vería en t2)


def test_simultaneous_entries_while_short_queue_long_reversal() -> None:
    el = _series([False, True])
    es = _series([True, True])
    pos = position_from_long_short(el, _false(2), es, _false(2))
    assert pos.to_list() == [-1, 0]  # t1: gana enter_long -> plano con pending=+1


def test_output_never_triggers_invalid_signals_in_position_to_signals() -> None:
    # Prueba exhaustiva (3 barras x 4 banderas = 2^12 = 4096 combinaciones, cubre toda
    # transición de 2 pasos): el guard de position_to_signals (Enmienda G2b) nunca debe
    # lanzar invalid_signals sobre una posición que salió de esta máquina de estados.
    for combo in itertools.product([False, True], repeat=12):
        cols = [pl.Series(list(combo[i::4]), dtype=pl.Boolean) for i in range(4)]
        pos = position_from_long_short(*cols)
        position_to_signals(pos)  # no debe lanzar
