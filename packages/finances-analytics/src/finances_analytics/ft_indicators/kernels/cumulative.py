"""Kernel acumulado: ``obv`` (RF-2).

OBV_0 = volume_0 (TA-Lib); OBV_t = OBV_{t−1} ± volume_t según C_t >/< C_{t−1} (igual si no
cambia). Fila con ``close`` o ``volume`` inválido → nulo, sin alterar el acumulado; el
cierre de referencia es el de la fila válida anterior. La semilla no decae nunca
(``stable_from_ms`` nulo, ``non_decaying_seed``).
"""

from __future__ import annotations

import polars as pl

from .common import COMPUTE_DTYPE, Outputs, Params, Program, all_valid, previous_valid


def obv(program: Program, params: Params, dtype: type[pl.DataType] = COMPUTE_DTYPE) -> Outputs:
    """On-Balance Volume con suma acumulada secuencial (igual que el bucle de TA-Lib)."""
    del params  # sin parámetros (RF-2)
    close = program.float_input("close", dtype)
    volume = program.float_input("volume", dtype)
    valid = program.let(all_valid(close, volume))
    valid_close = program.let(pl.when(valid).then(close))
    prev_close = previous_valid(program, valid_close)
    step = (
        pl.when(~valid)
        .then(None)
        .when(prev_close.is_null())
        .then(volume)
        .when(valid_close > prev_close)
        .then(volume)
        .when(valid_close < prev_close)
        .then(-volume)
        .otherwise(0.0)
    )
    return {"": step.cum_sum()}


__all__ = ["obv"]
