"""Tolerancias esperadas para implementaciones ``float32`` (RF-17, plan §3.9): fuente única.

Se comparan solo filas desde ``stable_from_ms`` con
``|x_f32 − x_oráculo| ≤ atol + atol_scaled·escala + rtol·|x_oráculo|``, donde ``escala`` es la
media de ``|source|`` de la serie (``close`` en los indicadores OHLC) o ``Σ|volume|`` en
``obv``. Nulos y convenciones de división por cero se exigen **exactos** (fuera de tabla).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from .catalog import ResolvedIndicator


@dataclass(frozen=True)
class Tolerance:
    """Tolerancia de una columna para una implementación ``f32``."""

    rtol: float
    atol: float = 0.0
    atol_scaled: float = 0.0  # × escala de la serie
    bit_exact: str = "No"
    reason: str = ""

    def allowed(self, reference: float, series_scale: float) -> float:
        """Error absoluto admitido frente al valor del oráculo ``reference``."""
        return self.atol + self.atol_scaled * series_scale + self.rtol * abs(reference)

    def as_dict(self) -> dict[str, float]:
        """Forma publicada en ``GET /catalog`` y en el paquete del oráculo."""
        return {"rtol": self.rtol, "atol": self.atol, "atol_scaled": self.atol_scaled}


_WINDOW_SUM = Tolerance(1e-5, reason="Sumas de ventana en f32", bit_exact="No (orden de suma)")
_BANDS = Tolerance(
    1e-5,
    atol_scaled=1e-5,
    reason="sqrt de varianza; cancelación si se usa E[x²]−E[x]²",
)
_MACD = Tolerance(0.0, atol_scaled=1e-5, reason="Diferencia de EMAs cercanas (cancelación)")
_ADX = Tolerance(0.0, atol=1e-2, reason="Divisiones encadenadas y doble suavizado")
_STOCH = Tolerance(0.0, atol=1e-3, reason="División por HH−LL pequeño")

#: Indicador → clave de salida (sufijo, o el nombre si la salida es la columna base) → tolerancia.
TOLERANCES: Mapping[str, Mapping[str, Tolerance]] = {
    "sma": {"sma": _WINDOW_SUM},
    "ema": {
        "ema": Tolerance(
            1e-5,
            bit_exact="Solo en la misma secuencia de operaciones",
            reason="Recursión; error de semilla < ε desde stable_from",
        )
    },
    "wma": {"wma": _WINDOW_SUM},
    "rsi": {"rsi": Tolerance(0.0, atol=1e-3, reason="División G/(G+L)")},
    "macd": {"line": _MACD, "signal": _MACD, "hist": _MACD},
    "bb": {"middle": _WINDOW_SUM, "upper": _BANDS, "lower": _BANDS},
    "atr": {"atr": Tolerance(1e-5, reason="Recursión de Wilder")},
    "stoch": {"k": _STOCH, "d": _STOCH},
    "roc": {"roc": Tolerance(0.0, atol=1e-4, reason="Cociente")},
    "obv": {"obv": Tolerance(0.0, atol_scaled=1e-6, reason="Acumulado largo en f32")},
    "adx": {"adx": _ADX, "plus_di": _ADX, "minus_di": _ADX},
    "vwap": {"vwap": _WINDOW_SUM},
}


def output_key(name: str, suffix: str) -> str:
    """Clave publicada de una salida: el sufijo, o el nombre del indicador si es la columna base."""
    return suffix or name


def tolerance(name: str, suffix: str) -> Tolerance:
    """Tolerancia de la salida ``suffix`` del indicador ``name``.

    Raises:
        KeyError: SI la salida no está en la tabla.
    """
    return TOLERANCES[name][output_key(name, suffix)]


def scale(resolved: ResolvedIndicator, frame: pl.DataFrame) -> float:
    """Escala de la serie para ``atol_scaled``: ``Σ|volume|`` (``obv``) o media de ``|precio|``."""
    if resolved.definition.name == "obv":
        value = frame.select(pl.col("volume").abs().sum()).item()
    else:
        price = resolved.inputs[0] if len(resolved.inputs) == 1 else "close"
        value = frame.select(pl.col(price).abs().mean()).item()
    return float(value) if value is not None else 0.0


__all__ = ["TOLERANCES", "Tolerance", "output_key", "scale", "tolerance"]
