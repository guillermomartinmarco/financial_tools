"""Utilidades de test compartidas por los tests de kernels, paridad y propiedades."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from fractions import Fraction
from pathlib import Path

import polars as pl

from finances_analytics.ft_indicators.kernels.common import COMPUTE_DTYPE, Outputs, Params, evaluate

REFERENCE_DIR = Path(__file__).resolve().parent / "fixtures" / "reference"
JAN_2024_MS = 1_704_067_200_000
HOUR_MS = 3_600_000

KernelFn = Callable[..., Outputs]


def apply_kernel(
    kernel: KernelFn,
    params: Params,
    frame: pl.DataFrame,
    dtype: type[pl.DataType] = COMPUTE_DTYPE,
) -> pl.DataFrame:
    """Evalúa ``kernel`` sobre ``frame``; la salida única se llama ``value``."""
    return evaluate(frame, kernel, params, dtype)


def read_reference(name: str) -> pl.DataFrame:
    """Lee un *fixture* de referencia (cabecera ``#`` con la fuente) como texto."""
    return pl.read_csv(REFERENCE_DIR / name, comment_prefix="#", infer_schema=False)


def as_floats(df: pl.DataFrame, column: str) -> list[float | None]:
    """Columna de un *fixture* a floats (``a/b`` exacto vía ``Fraction``; vacío = nulo)."""
    out: list[float | None] = []
    for raw in df[column].to_list():
        out.append(None if raw is None or raw == "" else float(Fraction(raw)))
    return out


def ohlcv_frame(columns: Mapping[str, list[float | None]]) -> pl.DataFrame:
    """Marco con ``open_time_ms`` horario y las columnas dadas en ``Float32``."""
    n = len(next(iter(columns.values())))
    data: dict[str, pl.Series] = {
        "open_time_ms": pl.Series(
            "open_time_ms", [JAN_2024_MS + i * HOUR_MS for i in range(n)], dtype=pl.Int64
        )
    }
    for name, values in columns.items():
        data[name] = pl.Series(name, values, dtype=pl.Float32)
    return pl.DataFrame(data)


def float64_frame(columns: Mapping[str, list[float | None]]) -> pl.DataFrame:
    """Como :func:`ohlcv_frame` pero sin pérdida (para *fixtures* publicados en doble precisión)."""
    n = len(next(iter(columns.values())))
    data: dict[str, pl.Series] = {
        "open_time_ms": pl.Series(
            "open_time_ms", [JAN_2024_MS + i * HOUR_MS for i in range(n)], dtype=pl.Int64
        )
    }
    for name, values in columns.items():
        data[name] = pl.Series(name, values, dtype=pl.Float64)
    return pl.DataFrame(data)


def random_walk(
    n: int, *, seed: int = 11, start: float = 100.0, scale: float = 1.0
) -> dict[str, list[float | None]]:
    """OHLCV determinista (paseo aleatorio); ``scale`` multiplica precios (precios extremos)."""
    rng = random.Random(seed)
    closes: list[float] = []
    price = start
    for _ in range(n):
        price = max(0.01, price * (1.0 + rng.gauss(0.0, 0.01)))
        closes.append(price)
    opens = [start, *closes[:-1]]
    highs = [
        max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.004))) for o, c in zip(opens, closes, strict=True)
    ]
    lows = [
        min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.004))) for o, c in zip(opens, closes, strict=True)
    ]
    volumes = [abs(rng.gauss(1000.0, 250.0)) for _ in range(n)]
    return {
        "open": [v * scale for v in opens],
        "high": [v * scale for v in highs],
        "low": [v * scale for v in lows],
        "close": [v * scale for v in closes],
        "volume": list(volumes),
    }


def flat_series(n: int, price: float = 100.0) -> dict[str, list[float | None]]:
    """Serie plana (O = H = L = C constante)."""
    return {
        "open": [price] * n,
        "high": [price] * n,
        "low": [price] * n,
        "close": [price] * n,
        "volume": [1000.0] * n,
    }
