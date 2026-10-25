"""Convergencia de la semilla (RF-16, plan §3.9): ``k_star``, ``stable_index``, ``stability``.

Regla analítica (sin mirar los datos): una etapa de suavizado exponencial con factor α
sembrada en el índice ``s`` deja a la semilla un peso ``(1−α)^k`` tras ``k`` pasos;
``k* = ⌈ln ε / ln(1−α)⌉`` (el menor ``k`` con ``(1−α)^k < ε``), ``ε = 1e-6``. Índice estable
de la etapa = ``max(s, índice estable de sus entradas) + k*``; una etapa sin memoria
(combinación de otras) toma el máximo de sus entradas.

Los índices cuentan solo filas válidas de la entrada (RF-3) y se traducen al
``open_time_ms`` de esa fila.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from finances_core.schemas.indicators import Stability

#: Peso residual de la semilla tolerado (parte de la versión del motor).
EPSILON = 1e-6

StabilityKind = Literal["recursive", "windowed", "non_decaying"]


def k_star(alpha: float, eps: float = EPSILON) -> int:
    """Menor ``k ≥ 1`` con ``(1−α)^k < eps`` (= ``⌈ln eps / ln(1−α)⌉`` salvo empates exactos).

    Raises:
        ValueError: SI ``alpha`` no está en ``(0, 1)`` o ``eps`` no está en ``(0, 1)``.
    """
    if not (0.0 < alpha < 1.0) or not (0.0 < eps < 1.0):
        msg = f"alpha={alpha} y eps={eps} deben estar en (0, 1)"
        raise ValueError(msg)
    decay = 1.0 - alpha
    k = max(1, math.ceil(math.log(eps) / math.log(decay)))
    while decay**k >= eps:
        k += 1
    while k > 1 and decay ** (k - 1) < eps:
        k -= 1
    return k


@dataclass(frozen=True)
class Stage:
    """Etapa del grafo de suavizado de una columna.

    ``alpha=None`` = combinación sin memoria de sus ``inputs`` (p. ej. MACD line = fast − slow).
    """

    seed_index: int = 0
    alpha: float | None = None
    alpha_text: str = ""
    inputs: tuple[Stage, ...] = field(default=())

    def describe(self) -> str:
        """Regla legible (p. ej. ``ewm alpha=2/21 eps=1e-6 seed_idx=19 k=139``)."""
        inner = ", ".join(s.describe() for s in self.inputs)
        if self.alpha is None:
            return f"max({inner})"
        text = (
            f"ewm alpha={self.alpha_text or self.alpha} eps={EPSILON:g} "
            f"seed_idx={self.seed_index} k={k_star(self.alpha)}"
        )
        return f"{text} after [{inner}]" if inner else text


def stable_index(stage: Stage) -> int:
    """Índice (en filas válidas) desde el que la columna no depende de su semilla."""
    base = max([stage.seed_index, *(stable_index(s) for s in stage.inputs)])
    return base + (k_star(stage.alpha) if stage.alpha is not None else 0)


class ResolvedLike(Protocol):
    """Lo que :func:`stability` necesita de un indicador resuelto (sin importar ``catalog``)."""

    @property
    def kind(self) -> StabilityKind: ...

    @property
    def columns(self) -> Mapping[str, str]: ...

    @property
    def stages(self) -> Mapping[str, Stage]: ...


def stability(
    resolved: ResolvedLike,
    valid_times: Sequence[int],
    first_non_null: Mapping[str, int | None],
) -> Stability:
    """``Stability`` (RF-16) de un indicador calculado.

    Args:
        resolved: indicador resuelto (columnas y grafo de etapas).
        valid_times: ``open_time_ms`` de las filas válidas de la entrada, en orden.
        first_non_null: ``open_time_ms`` de la primera fila no nula por columna (no recursivos).
    """
    columns = list(resolved.columns.values())
    if resolved.kind == "non_decaying":
        return Stability(
            stable_from_ms=None,
            by_column=dict.fromkeys(columns),
            rule="non_decaying_seed: el nivel depende para siempre de la primera fila",
            reason="non_decaying_seed",
        )
    by_column: dict[str, int | None] = {}
    if resolved.kind == "windowed":
        for col in columns:
            by_column[col] = first_non_null.get(col)
        rule = "first_non_null"
        converged_reason: Literal["first_non_null", "converged"] = "first_non_null"
    else:
        rules = []
        for col in columns:
            stage = resolved.stages[col]
            idx = stable_index(stage)
            by_column[col] = valid_times[idx] if idx < len(valid_times) else None
            rules.append(f"{col}: idx={idx} {stage.describe()}")
        rule = "; ".join(rules)
        converged_reason = "converged"
    values = list(by_column.values())
    if any(v is None for v in values):
        return Stability(
            stable_from_ms=None, by_column=by_column, rule=rule, reason="not_converged"
        )
    return Stability(
        stable_from_ms=max(v for v in values if v is not None),
        by_column=by_column,
        rule=rule,
        reason=converged_reason,
    )


__all__ = [
    "EPSILON",
    "ResolvedLike",
    "Stage",
    "StabilityKind",
    "k_star",
    "stability",
    "stable_index",
]
