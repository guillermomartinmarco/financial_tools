"""Oráculo (L2.oracle): harness de cross-validación viva de ``strategies/_lib`` vs TA-Lib.

``finances_core`` es el *bridge* (Art. 3) y **no** depende de ``talib``; sus ayudas de
estrategias (``finances_core.backtest.strategies._lib``) son ``pl.Expr``/``pl.Series`` en
``Float32`` validadas con *golden* hand-computed en ``finances-core`` (sin ``talib``). Este
módulo vive en ``finances-analytics`` —el único paquete con ``ta-lib`` como dependencia
directa— y agrega la capa **viva**: corre cada ayuda de ``_lib`` sobre una serie OHLCV
determinista y la compara con TA-Lib dentro de la tolerancia ``Float32`` (RFC-0004). Caza
*drift* de versión de TA-Lib y errores de fórmula que un *golden* aislado podría no ver.

Decisiones de diseño:

* **Serie determinista reutilizada**: :func:`ohlcv` envuelve el paseo aleatorio con semilla
  fija ya probado del repo (``tests/datasets.random_walk_ohlcv``) y lo castea a ``Float32``
  (la entrada real de ``_lib``). TA-Lib recibe los mismos números en ``float64``
  (:func:`talib_inputs`): la única diferencia esperada es el redondeo ``Float32`` de salida.
* **Alineación de calentamiento**: nuestras ayudas devuelven ``null`` en el calentamiento;
  TA-Lib devuelve ``NaN``. :func:`assert_matches` exige que, desde ``compare_from``, ninguno
  tenga ``null``/``NaN`` donde el otro tiene un número finito, y compara el resto con la
  tolerancia escalada.
* **Tolerancia RFC-0004**: ``|x − ref| ≤ atol + rtol·|ref|`` con un piso ``atol`` relativo a
  la escala de precio; nunca se exige igualdad bit a bit entre float64 (TA-Lib) y Float32.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from ..datasets import random_walk_ohlcv

#: Filas de la serie del oráculo: holgadas para pasar el calentamiento más largo de L2
#: (``senkou_b=52`` de Ichimoku, ``2n−1`` del doble Wilder de ADX, etc.).
ROWS = 600
#: Semilla fija (determinista entre corridas y máquinas).
SEED = 20_261_111
#: Epsilon de ``Float32`` (2⁻²³ ≈ 1.19e-7).
F32_EPS = float(np.finfo(np.float32).eps)

_COLUMNS = ("open", "high", "low", "close", "volume")


def ohlcv(rows: int = ROWS, *, seed: int = SEED) -> dict[str, pl.Series]:
    """Serie OHLCV determinista como ``pl.Series`` ``Float32`` (la entrada real de ``_lib``).

    Reusa el generador con semilla fija del repo (invariantes OHLC garantizados:
    ``high ≥ max(open, close)``, ``low ≤ min(open, close)``, ``volume > 0``).
    """
    frame = random_walk_ohlcv(rows, seed=seed)
    return {name: frame[name].cast(pl.Float32).rename(name) for name in _COLUMNS}


def talib_inputs(series: dict[str, pl.Series]) -> dict[str, np.ndarray]:
    """Mismos números que :func:`ohlcv` en ``float64`` contiguo, como los espera TA-Lib."""
    return {name: col.cast(pl.Float64).to_numpy() for name, col in series.items()}


def price_scale(series: dict[str, pl.Series]) -> float:
    """Escala de precio (media de ``|close|``) para el piso ``atol`` de la tolerancia."""
    return float(series["close"].abs().mean() or 1.0)


def assert_matches(
    ours: pl.Series,
    ref: np.ndarray,
    *,
    compare_from: int,
    rtol: float = 2e-4,
    atol: float = 1e-3,
    excluded_rows: frozenset[int] = frozenset(),
) -> None:
    """Falla si ``ours`` (``_lib``) y ``ref`` (TA-Lib) difieren más que la tolerancia F32.

    * ``compare_from``: primera fila estable a comparar (p. ej. ``2n−1`` en ADX). Antes de
      esa fila no se compara (calentamiento no acordado entre ambas semillas).
    * Para ``t >= compare_from`` (salvo ``excluded_rows``): exige que ``ours[t]`` no sea
      ``null`` **y** ``ref[t]`` sea finito, y que ``|ours[t] − ref[t]| ≤ atol + rtol·|ref[t]|``.
    * ``excluded_rows``: filas con divergencia documentada (p. ej. semilla distinta de un
      indicador puntual); se saltean con una razón explícita en el test que las declara.
    """
    assert ours.dtype == pl.Float32, f"salida de _lib no es Float32: {ours.dtype}"
    values = ours.to_list()
    n = len(values)
    assert n == len(ref), f"largos distintos: {n} vs {len(ref)}"
    mismatches: list[str] = []
    for t in range(compare_from, n):
        if t in excluded_rows:
            continue
        mine = values[t]
        theirs = float(ref[t])
        if mine is None:
            if np.isfinite(theirs):
                mismatches.append(f"t={t}: _lib=null pero TA-Lib={theirs:.6g}")
            continue
        if not np.isfinite(theirs):
            mismatches.append(f"t={t}: _lib={mine:.6g} pero TA-Lib={theirs!r}")
            continue
        if abs(mine - theirs) > atol + rtol * abs(theirs):
            mismatches.append(
                f"t={t}: _lib={mine:.6g} vs TA-Lib={theirs:.6g} "
                f"(|Δ|={abs(mine - theirs):.3g} > {atol + rtol * abs(theirs):.3g})"
            )
    assert not mismatches, "cross-validación vs TA-Lib falló:\n  " + "\n  ".join(mismatches[:20])
