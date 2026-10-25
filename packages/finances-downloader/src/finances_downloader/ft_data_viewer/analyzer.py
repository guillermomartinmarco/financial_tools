"""Diagnóstico de datasets OHLCV: resumen, gaps, anomalías y muestreo (RF-2..RF-5).

Todas las funciones reciben ``pl.LazyFrame`` y solo materializan agregados o el
subconjunto de filas pedido (DV-02); nunca el dataset completo.
"""

from __future__ import annotations

import math
import random
from datetime import timedelta
from typing import Any, Literal

import polars as pl
import structlog
from finances_core.schemas.data_viewer import (
    AnomalyReport,
    ColumnStats,
    DatasetIdentifier,
    DatasetSummary,
    GapInfo,
)
from finances_core.schemas.market_data import OHLCV_SCHEMA, TF_MS_MAP

logger = structlog.get_logger(__name__, component="ft_data_viewer")

TS_COL = "open_time_ms"
CRITICAL_COLS = ("open", "high", "low", "close", "volume")
PRICE_COLS = ("open", "high", "low", "close")
VOLUME_COL = "volume"
#: Umbral Z-score para outliers (RF-2, RF-5).
ZSCORE_THRESHOLD = 3.0
#: Factor del método IQR (Tukey).
IQR_FACTOR = 1.5
#: Un salto se considera gap si supera 1.5 velas (tolera meses de 28-31 días en ``1M``).
GAP_TOLERANCE = 1.5
#: Máximo de ejemplos por categoría en ``AnomalyReport`` (los conteos no se truncan).
MAX_EXAMPLES = 100
MAX_DUPLICATE_EXAMPLES = 10

OutlierMethod = Literal["zscore", "iqr"]
SampleMode = Literal["head", "tail", "sample"]

_NUMERIC = (pl.Float32, pl.Float64, pl.Int32, pl.Int64, pl.UInt32, pl.UInt64)


def _ordered_columns(schema: pl.Schema) -> list[str]:
    """Columnas en orden canónico (``OHLCV_SCHEMA``) seguido del resto."""
    names = schema.names()
    canonical = [c for c in OHLCV_SCHEMA if c in names]
    return canonical + [c for c in names if c not in canonical]


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def compute_summary(
    lf: pl.LazyFrame,
    identifier: DatasetIdentifier,
    *,
    metadata: dict[str, ColumnStats] | None = None,
    gap_threshold_ms: int = 0,
) -> DatasetSummary:
    """Resumen estadístico en una única pasada agregada.

    ``min``/``max``/``null_count`` se toman de ``metadata`` (estadísticas Parquet) cuando
    se proporciona; ``mean``/``std``/``distinct_count`` y los conteos de gaps, duplicados
    y outliers requieren leer las columnas implicadas (proyección lazy).
    """
    schema = lf.collect_schema()
    columns = _ordered_columns(schema)
    meta = metadata or {}

    exprs: list[pl.Expr] = [
        pl.len().alias("__rows"),
        pl.col(TS_COL).min().alias("__min_ts"),
        pl.col(TS_COL).max().alias("__max_ts"),
    ]
    for name in columns:
        col = pl.col(name)
        exprs.append(col.n_unique().alias(f"{name}__distinct"))
        if name not in meta:
            exprs.append(col.null_count().alias(f"{name}__nulls"))
        if schema[name] in _NUMERIC:
            exprs += [col.mean().alias(f"{name}__mean"), col.std().alias(f"{name}__std")]
            if name not in meta:
                exprs += [col.min().alias(f"{name}__min"), col.max().alias(f"{name}__max")]
    agg = lf.select(exprs).collect().row(0, named=True)

    stats: list[ColumnStats] = []
    for name in columns:
        known = meta.get(name)
        numeric = schema[name] in _NUMERIC
        stats.append(
            ColumnStats(
                name=name,
                dtype=str(schema[name]),
                null_count=known.null_count if known else int(agg[f"{name}__nulls"]),
                distinct_count=int(agg[f"{name}__distinct"]),
                min=(known.min if known else _f(agg[f"{name}__min"])) if numeric else None,
                max=(known.max if known else _f(agg[f"{name}__max"])) if numeric else None,
                mean=_f(agg[f"{name}__mean"]) if numeric else None,
                std=_f(agg[f"{name}__std"]) if numeric else None,
            )
        )

    timeframe_ms = _timeframe_ms(identifier.timeframe)
    return DatasetSummary(
        dataset_id=identifier,
        row_count=int(agg["__rows"]),
        date_range=(int(agg["__min_ts"] or 0), int(agg["__max_ts"] or 0)),
        columns=stats,
        gaps_detected=len(detect_gaps(lf, timeframe_ms, 1, gap_threshold_ms)),
        duplicate_timestamps=_duplicate_stats(lf)[0],
        outlier_count=count_price_outliers(lf),
    )


def _timeframe_ms(timeframe: str) -> int:
    try:
        return TF_MS_MAP[timeframe]
    except KeyError:
        msg = f"timeframe desconocido: {timeframe!r}"
        raise ValueError(msg) from None


# --- Gaps (RF-4) ----------------------------------------------------------------------


def detect_gaps(
    lf: pl.LazyFrame,
    timeframe_ms: int,
    min_gap_candles: int = 1,
    gap_threshold_ms: int = 0,
) -> list[GapInfo]:
    """Detecta intervalos faltantes comparando timestamps consecutivos (ordenados, únicos).

    Un gap se reporta SI ``diff > 1.5 * timeframe_ms`` Y ``missing >= min_gap_candles``
    Y la duración faltante ``>= gap_threshold_ms`` (``FT_DATA_VIEWER_GAP_THRESHOLD_MS``).
    """
    if timeframe_ms <= 0:
        msg = "timeframe_ms debe ser > 0"
        raise ValueError(msg)
    if min_gap_candles < 1:
        msg = "min_gap_candles debe ser >= 1"
        raise ValueError(msg)

    ts = pl.col(TS_COL)
    prev = pl.col("__prev")
    diff = ts - prev
    missing = ((diff / timeframe_ms).round() - 1).cast(pl.Int64).clip(lower_bound=1)
    gaps = (
        lf.select(ts)
        .filter(ts.is_not_null())
        .unique()
        .sort(TS_COL)
        .with_columns(ts.shift(1).alias("__prev"))
        .filter(diff > timeframe_ms * GAP_TOLERANCE)
        .select(
            (prev + timeframe_ms).alias("gap_start_ms"),
            (ts - timeframe_ms).alias("gap_end_ms"),
            missing.alias("missing_candles"),
            (diff - timeframe_ms).alias("missing_ms"),
        )
        .filter(
            (pl.col("missing_candles") >= min_gap_candles)
            & (pl.col("missing_ms") >= gap_threshold_ms)
        )
        .collect()
    )
    return [
        GapInfo(
            gap_start_ms=row["gap_start_ms"],
            gap_end_ms=row["gap_end_ms"],
            missing_candles=row["missing_candles"],
            gap_duration_human=format_duration_human(row["missing_ms"]),
        )
        for row in gaps.iter_rows(named=True)
    ]


# --- Duplicados / outliers / anomalías (RF-5) -----------------------------------------


def _duplicates_frame(lf: pl.LazyFrame) -> pl.LazyFrame:
    return lf.group_by(TS_COL).agg(pl.len().alias("count")).filter(pl.col("count") > 1)


def _duplicate_stats(lf: pl.LazyFrame) -> tuple[int, int]:
    """(timestamps duplicados, filas sobrantes), sin truncar."""
    row = (
        _duplicates_frame(lf)
        .select(pl.len().alias("n"), (pl.col("count") - 1).sum().alias("extra"))
        .collect()
        .row(0)
    )
    return int(row[0]), int(row[1] or 0)


def _moments(lf: pl.LazyFrame, cols: list[str]) -> dict[str, tuple[float | None, float | None]]:
    if not cols:
        return {}
    exprs = [
        e
        for c in cols
        for e in (pl.col(c).mean().alias(f"{c}__m"), pl.col(c).std().alias(f"{c}__s"))
    ]
    row = lf.select(exprs).collect().row(0, named=True)
    return {c: (_f(row[f"{c}__m"]), _f(row[f"{c}__s"])) for c in cols}


def _outlier_bounds(
    lf: pl.LazyFrame, cols: list[str], method: OutlierMethod
) -> dict[str, tuple[float, float]]:
    """Límites [lo, hi] por columna; fuera de ellos un valor es outlier."""
    bounds: dict[str, tuple[float, float]] = {}
    if method == "zscore":
        for c, (mean, std) in _moments(lf, cols).items():
            if mean is not None and std:
                bounds[c] = (mean - ZSCORE_THRESHOLD * std, mean + ZSCORE_THRESHOLD * std)
        return bounds
    if not cols:
        return bounds
    exprs = [
        e
        for c in cols
        for e in (
            pl.col(c).quantile(0.25).alias(f"{c}__q1"),
            pl.col(c).quantile(0.75).alias(f"{c}__q3"),
        )
    ]
    row = lf.select(exprs).collect().row(0, named=True)
    for c in cols:
        q1, q3 = _f(row[f"{c}__q1"]), _f(row[f"{c}__q3"])
        if q1 is not None and q3 is not None:
            iqr = q3 - q1
            bounds[c] = (q1 - IQR_FACTOR * iqr, q3 + IQR_FACTOR * iqr)
    return bounds


def _present(lf: pl.LazyFrame, cols: tuple[str, ...]) -> list[str]:
    names = lf.collect_schema().names()
    return [c for c in cols if c in names]


def count_price_outliers(lf: pl.LazyFrame, method: OutlierMethod = "zscore") -> int:
    """Total de valores outlier en ``open/high/low/close`` (sin truncar)."""
    bounds = _outlier_bounds(lf, _present(lf, PRICE_COLS), method)
    if not bounds:
        return 0
    exprs = [((pl.col(c) < lo) | (pl.col(c) > hi)).sum().alias(c) for c, (lo, hi) in bounds.items()]
    return int(sum(lf.select(exprs).collect().row(0)))


def detect_anomalies(
    lf: pl.LazyFrame,
    identifier: DatasetIdentifier,
    outlier_method: OutlierMethod = "zscore",
) -> AnomalyReport:
    """Duplicados, nulos en columnas críticas, outliers de precio y volumen anómalo (RF-5)."""
    if outlier_method not in ("zscore", "iqr"):
        msg = f"outlier_method inválido: {outlier_method!r} (zscore|iqr)"
        raise ValueError(msg)

    dup_count, extra_rows = _duplicate_stats(lf)
    examples = _duplicates_frame(lf).sort(TS_COL).limit(MAX_DUPLICATE_EXAMPLES).collect()
    duplicate_timestamps: dict[str, Any] = {
        "count": dup_count,
        "extra_rows": extra_rows,
        "examples": [
            {"timestamp": r[TS_COL], "count": r["count"]} for r in examples.iter_rows(named=True)
        ],
    }

    critical = _present(lf, CRITICAL_COLS)
    nulls_row = (
        lf.select([pl.col(c).null_count().alias(c) for c in critical]).collect().row(0, named=True)
        if critical
        else {}
    )
    null_values = {c: int(n) for c, n in nulls_row.items() if n}

    prices = _present(lf, PRICE_COLS)
    moments = _moments(lf, prices)
    price_outliers: dict[str, list[dict[str, Any]]] = {}
    for c, (lo, hi) in _outlier_bounds(lf, prices, outlier_method).items():
        mean, std = moments[c]
        rows = (
            lf.filter((pl.col(c) < lo) | (pl.col(c) > hi))
            .select(TS_COL, c)
            .sort(TS_COL)
            .limit(MAX_EXAMPLES)
            .collect()
        )
        if rows.height:
            price_outliers[c] = [
                {
                    "timestamp": r[TS_COL],
                    "value": float(r[c]),
                    "zscore": abs((r[c] - mean) / std) if mean is not None and std else 0.0,
                    "method": outlier_method,
                }
                for r in rows.iter_rows(named=True)
            ]

    return AnomalyReport(
        dataset_id=identifier,
        duplicate_timestamps=duplicate_timestamps,
        null_values=null_values,
        price_outliers=price_outliers,
        volume_anomalies=_volume_anomalies(lf),
    )


def _volume_anomalies(lf: pl.LazyFrame) -> dict[str, list[dict[str, Any]]]:
    """Volumen == 0 o volumen > media + 3·std."""
    if VOLUME_COL not in lf.collect_schema().names():
        return {}
    mean, std = _moments(lf, [VOLUME_COL])[VOLUME_COL]
    vol = pl.col(VOLUME_COL)
    high = vol > (mean + ZSCORE_THRESHOLD * std) if mean is not None and std else pl.lit(False)
    rows = (
        lf.filter((vol == 0) | high)
        .select(TS_COL, VOLUME_COL)
        .sort(TS_COL)
        .limit(MAX_EXAMPLES)
        .collect()
    )
    anomalies: list[dict[str, Any]] = []
    for r in rows.iter_rows(named=True):
        value = float(r[VOLUME_COL])
        if value == 0:
            anomalies.append({"timestamp": r[TS_COL], "value": value, "type": "zero_volume"})
        else:
            z = (value - mean) / std if mean is not None and std else 0.0
            anomalies.append(
                {"timestamp": r[TS_COL], "value": value, "type": "high_volume", "zscore": z}
            )
    return {VOLUME_COL: anomalies} if anomalies else {}


# --- Muestreo (RF-3) ------------------------------------------------------------------


def reservoir_indices(total: int, k: int, rng: random.Random) -> list[int]:
    """Índices de una muestra uniforme de ``k`` sobre un stream de ``total`` filas.

    Reservoir sampling (familia Vitter R) con la optimización por saltos de Li
    (*Algorithm L*): misma distribución uniforme que R en O(k·(1 + log(total/k))).
    """
    if k <= 0 or total <= 0:
        return []
    if total <= k:
        return list(range(total))

    def u() -> float:
        while (x := rng.random()) == 0.0:
            pass
        return x

    reservoir = list(range(k))
    w = math.exp(math.log(u()) / k)
    i = k - 1
    while True:
        i += math.floor(math.log(u()) / math.log(1 - w)) + 1
        if i >= total:
            break
        reservoir[rng.randrange(k)] = i
        w *= math.exp(math.log(u()) / k)
    return sorted(reservoir)


def sample_rows(
    lf: pl.LazyFrame,
    n: int,
    mode: SampleMode,
    cols: list[str] | None = None,
    *,
    seed: int | None = None,
) -> pl.DataFrame:
    """``head``/``tail`` (orden por ``open_time_ms``) o muestra aleatoria reservoir.

    La proyección ``cols`` se aplica después de ordenar, por lo que no necesita incluir
    ``open_time_ms``. ``sample`` solo materializa las ``n`` filas elegidas.
    """
    if n < 1:
        msg = "n debe ser >= 1"
        raise ValueError(msg)
    if mode not in ("head", "tail", "sample"):
        msg = f"mode inválido: {mode!r} (head|tail|sample)"
        raise ValueError(msg)

    if mode == "head":
        out = lf.sort(TS_COL).limit(n)
    elif mode == "tail":
        out = lf.sort(TS_COL, descending=True).limit(n)
    else:
        total = int(lf.select(pl.len()).collect().item())
        chosen = reservoir_indices(total, n, random.Random(seed))
        out = lf.with_row_index("__row").filter(pl.col("__row").is_in(chosen)).drop("__row")
    if cols:
        out = out.select(cols)
    return out.collect()


def format_duration_human(ms: int) -> str:
    """Formatea milisegundos a string legible (``1d 2h 3m 4s``)."""
    td = timedelta(milliseconds=ms)
    hours, rem = divmod(td.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = [f"{v}{unit}" for v, unit in ((td.days, "d"), (hours, "h"), (minutes, "m")) if v]
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)
