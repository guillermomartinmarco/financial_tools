"""Evaluación honesta de una corrida: expectancy en R-múltiplos + bootstrap CI (Enmienda G5).

Puro sobre el contrato ``trades`` canónico (RF-6) más el ``sl_frac`` de la corrida. El
R-múltiplo de una operación es su PnL en unidades de "riesgo inicial":

    R[t] = pnl[t] / (entry_price[t] · size[t] · sl_frac)

Como ``pnl`` ya es neto de costos (RF-6), R es neto. ``expectancy_r`` es la media de R. El
intervalo de confianza sale de un **bootstrap IID** (remuestreo con reemplazo de las
operaciones), con semilla fija (determinista, Art. 8): dos corridas con la misma entrada y
semilla dan el mismo reporte. El criterio de "edge demostrado" es ``r_ci_low > 0``.

Precisión (RF-11): ``finances_core.metrics`` se mantiene en el límite numérico del contrato.
R y el bootstrap son **reducciones escalares en doble precisión** (floats de Python, igual que
``trading.py`` con ``expectancy``/``sqn``); no hay columnas ``float64`` ni arreglos NumPy — la
excepción de RFC-0004 es exclusiva de ``ft_vectorbt/engine.py`` y no alcanza a este paquete.
"""

from __future__ import annotations

import math
import random

import polars as pl
from pydantic import BaseModel, ConfigDict

__all__ = [
    "DEFAULT_CI_LEVEL",
    "DEFAULT_MIN_TRADES",
    "DEFAULT_N_RESAMPLES",
    "DEFAULT_SEED",
    "EvaluationReport",
    "compute_evaluation",
]

#: Nivel de confianza por defecto del intervalo bootstrap.
DEFAULT_CI_LEVEL = 0.95
#: Número de remuestreos por defecto.
DEFAULT_N_RESAMPLES = 10_000
#: Semilla por defecto (determinismo reproducible).
DEFAULT_SEED = 1234
#: Mínimo de operaciones para declarar un *edge*: con muy pocas el bootstrap es degenerado
#: (IC de ancho casi nulo) y afirmar *edge* sería un falso positivo (anti-p-hacking).
DEFAULT_MIN_TRADES = 30


class EvaluationReport(BaseModel):
    """Reporte de evaluación honesta de una corrida (Enmienda G5).

    Las métricas en R (``expectancy_r``, ``r_ci_low``/``r_ci_high``, ``has_edge``) solo están
    definidas si la corrida tuvo ``sl_frac``; sin stop son ``None``/``False``. ``expectancy_cash``
    (media de PnL) queda siempre como referencia.
    """

    model_config = ConfigDict(extra="forbid")

    n_trades: int
    sl_frac: float | None
    expectancy_cash: float | None
    expectancy_r: float | None
    r_ci_low: float | None
    r_ci_high: float | None
    ci_level: float
    n_resamples: int
    seed: int
    min_trades: int
    sufficient_sample: bool
    has_edge: bool


def _percentile(sorted_values: list[float], q: float) -> float:
    """Percentil ``q`` (0..1) de una lista **ya ordenada** por interpolación lineal.

    Coincide con el método por defecto de la literatura (``linear``): ``rank = q·(N-1)``.
    """
    size = len(sorted_values)
    if size == 1:
        return sorted_values[0]
    rank = q * (size - 1)
    low = int(math.floor(rank))
    if low >= size - 1:
        return sorted_values[-1]
    frac = rank - low
    return sorted_values[low] + frac * (sorted_values[low + 1] - sorted_values[low])


def _bootstrap_mean_ci(
    values: list[float], *, ci_level: float, n_resamples: int, seed: int
) -> tuple[float, float]:
    """IC percentil de la media por bootstrap IID (determinista, reducción escalar pura).

    Remuestrea ``values`` con reemplazo ``n_resamples`` veces con un RNG sembrado (Mersenne
    Twister de ``random``, estable y reproducible) y devuelve los percentiles ``(1-ci)/2`` y
    ``(1+ci)/2`` de las medias. Doble precisión escalar, sin NumPy (RF-11).
    """
    rng = random.Random(seed)
    randrange = rng.randrange
    size = len(values)
    means = sorted(
        math.fsum(values[randrange(size)] for _ in range(size)) / size for _ in range(n_resamples)
    )
    alpha = (1.0 - ci_level) / 2.0
    return _percentile(means, alpha), _percentile(means, 1.0 - alpha)


def compute_evaluation(  # noqa: PLR0913 - parámetros de configuración del reporte (keyword-only)
    trades: pl.DataFrame,
    *,
    sl_frac: float | None,
    ci_level: float = DEFAULT_CI_LEVEL,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = DEFAULT_SEED,
    min_trades: int = DEFAULT_MIN_TRADES,
) -> EvaluationReport:
    """Calcula el :class:`EvaluationReport` de una corrida desde ``trades`` + ``sl_frac``.

    Sin operaciones: todo ``None`` y ``has_edge = False``. Sin ``sl_frac``: solo
    ``expectancy_cash``; las métricas en R quedan ``None``. Con ambos: deriva R por operación,
    su media y el intervalo bootstrap. ``has_edge`` exige además ``n_trades >= min_trades``
    (``sufficient_sample``): con muy pocas operaciones el IC es degenerado y afirmar *edge* sería
    un falso positivo.
    """
    n = trades.height
    sufficient_sample = n >= min_trades
    if n == 0:
        return EvaluationReport(
            n_trades=0,
            sl_frac=sl_frac,
            expectancy_cash=None,
            expectancy_r=None,
            r_ci_low=None,
            r_ci_high=None,
            ci_level=ci_level,
            n_resamples=n_resamples,
            seed=seed,
            min_trades=min_trades,
            sufficient_sample=False,
            has_edge=False,
        )

    pnl = [float(x) for x in trades["pnl"].to_list()]
    expectancy_cash = math.fsum(pnl) / n
    if sl_frac is None:
        return EvaluationReport(
            n_trades=n,
            sl_frac=None,
            expectancy_cash=expectancy_cash,
            expectancy_r=None,
            r_ci_low=None,
            r_ci_high=None,
            ci_level=ci_level,
            n_resamples=n_resamples,
            seed=seed,
            min_trades=min_trades,
            sufficient_sample=sufficient_sample,
            has_edge=False,
        )

    # R por operación = PnL / riesgo inicial (entry_price·size·sl_frac), escalares de Python.
    entry_price = [float(x) for x in trades["entry_price"].to_list()]
    size = [float(x) for x in trades["size"].to_list()]
    r = [p / (e * s * sl_frac) for p, e, s in zip(pnl, entry_price, size, strict=True)]
    expectancy_r = math.fsum(r) / n
    low, high = _bootstrap_mean_ci(r, ci_level=ci_level, n_resamples=n_resamples, seed=seed)
    return EvaluationReport(
        n_trades=n,
        sl_frac=float(sl_frac),
        expectancy_cash=expectancy_cash,
        expectancy_r=expectancy_r,
        r_ci_low=low,
        r_ci_high=high,
        ci_level=ci_level,
        n_resamples=n_resamples,
        seed=seed,
        min_trades=min_trades,
        sufficient_sample=sufficient_sample,
        has_edge=sufficient_sample and low > 0.0,
    )
