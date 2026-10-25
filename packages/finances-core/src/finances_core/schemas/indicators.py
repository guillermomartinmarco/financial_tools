"""Contratos de indicadores (plan de ``ft_indicators`` §1.1–§1.2, RF-4).

Viven en ``finances_core`` porque ``ft_indicators``, ``ft_ta_lib`` y el futuro
backtester los comparten sin importarse entre sí (Art. 4):

- **Genéricos** (ambas herramientas): ``Stability``, ``StoreEntry``, ``StoreManifest``,
  ``ComputeProgress``, ``ComputeItemResult``, ``ComputeResult``, ``DispatchResponse``,
  ``SeriesExportRequest`` y la nomenclatura ``format_param``/``series_id``/``output_columns``.
- **Propios de ``ft_indicators``**: ``IndicatorSpec``, ``IndicatorComputeRequest``,
  ``IndicatorPlanItem``, ``IndicatorPlan``, ``IndicatorCatalogEntry``, ``TalibEquivalentInfo``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Literal

import polars as pl
from pydantic import BaseModel, Field

from finances_core.export import ExportFormat
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.dispatch import DispatchResponse

__all__ = [
    "EXPORT_ID_COLUMNS",
    "OHLCV_EXPORT_COLUMNS",
    "SERIES_FILE_KEY",
    "ColumnDtype",
    "ComputeItemResult",
    "ComputePhase",
    "ComputeProgress",
    "ComputeResult",
    "DispatchResponse",
    "IndicatorCatalogEntry",
    "IndicatorComputeRequest",
    "IndicatorName",
    "IndicatorPlan",
    "IndicatorPlanItem",
    "IndicatorSpec",
    "Namespace",
    "PriceSource",
    "SeriesExportRequest",
    "Stability",
    "StabilityMode",
    "StabilityReason",
    "StoreEntry",
    "StoreManifest",
    "TalibEquivalentInfo",
    "format_param",
    "output_columns",
    "series_id",
]

# --------------------------------------------------------------------- genéricos

Namespace = Literal["ft_indicators", "ft_ta_lib"]
ComputePhase = Literal["PENDING", "PLANNING", "COMPUTING", "COMPLETED", "FAILED"]
PriceSource = Literal["open", "high", "low", "close", "adj_close"]
StabilityReason = Literal[
    "converged", "first_non_null", "non_decaying_seed", "not_converged", "unstable_period_unknown"
]
StabilityMode = Literal["all", "flag", "drop"]
#: Tipos persistidos permitidos (Art. 5.1 y 5.1 bis): reales, categóricas e índices en ms.
ColumnDtype = Literal["Float32", "Int16", "Int64"]


class Stability(BaseModel):
    """RF-16: desde qué vela cada columna deja de depender de su semilla."""

    stable_from_ms: int | None  # serie = máximo de sus columnas
    by_column: dict[str, int | None]
    rule: str  # p. ej. "ewm alpha=2/21 eps=1e-6 seed_idx=19 k=139"
    reason: StabilityReason


class StoreEntry(BaseModel):
    """Una serie persistida (un indicador de ``ft_indicators`` / una función de TA-Lib)."""

    series_id: str  # = indicator_id en ft_indicators
    spec: dict[str, object]  # spec resuelta propia de la herramienta
    columns: list[str]
    column_dtypes: dict[str, ColumnDtype]
    engine_version: str  # p. ej. "ft_indicators:3" o "ta-lib 0.8.2 / C 0.8.1"
    rows: int
    first_ms: int | None
    last_ms: int | None  # detección de desactualización
    stability: Stability
    computed_at_ms: int


class StoreManifest(BaseModel):
    """Manifiesto ``_manifest.{namespace}.json`` de un dataset (``GET /datasets/{...}``)."""

    namespace: Namespace
    dataset: DatasetIdentifier
    lock_task_id: str | None = None
    entries: dict[str, StoreEntry] = Field(default_factory=dict)


class ComputeProgress(BaseModel):
    task_id: str
    status: ComputePhase
    datasets_total: int
    datasets_done: int
    items_total: int
    items_done: int
    rows_computed: int
    partitions_written: int
    stability: dict[str, Stability]  # clave "ex/mt/sym/tf:series_id"
    warnings: list[str]
    errors: list[str]


class ComputeItemResult(BaseModel):
    dataset: DatasetIdentifier
    series_id: str
    columns: list[str]
    rows: int
    first_ms: int | None
    last_ms: int | None
    stability: Stability
    partitions_written: int
    partitions_removed: int
    warnings: list[str]  # insufficient_history, not_converged, null_inputs=N


class ComputeResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    items: list[ComputeItemResult]
    output_dirs: list[str]
    warnings: list[str]
    errors: list[str]


class SeriesExportRequest(BaseModel):
    start_ms: int | None = None
    end_ms: int | None = None  # exclusivo
    series: list[str] | None = None  # series_id; None = todas las del namespace
    columns: list[str] | None = None
    include_ohlcv: bool = False
    stability: StabilityMode = "all"
    format: ExportFormat = "parquet"
    output_dir: str | None = None  # None/"-" = stream


# ------------------------------------------------------------- esquemas Polars (§1.2)

#: Clave de cada archivo de serie (+ columnas de salida con el dtype de la herramienta).
SERIES_FILE_KEY: dict[str, type[pl.DataType]] = {"open_time_ms": pl.Int64}
EXPORT_ID_COLUMNS = ["exchange", "market_type", "symbol", "timeframe", "open_time_ms"]
#: ``adj_close`` solo si existe en la fuente.
OHLCV_EXPORT_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]

# --------------------------------------------------------------- nomenclatura RF-4

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9]*_)?$")
_TOKEN_STR_RE = re.compile(r"^[a-z0-9_]+$")
#: Decimales máximos representables en un ``series_id``.
MAX_PARAM_DECIMALS = 6


def format_param(value: int | float | str) -> str:
    """Token de un parámetro en el ``series_id`` (RF-4).

    ``.`` → ``p``, negativo → prefijo ``m``, sin ceros sobrantes (``2.0`` → ``2``,
    ``-0.5`` → ``m0p5``), máximo 6 decimales; cadenas en minúsculas ``[a-z0-9_]``.

    Raises:
        ValueError: SI el valor es booleano, no finito, tiene más de 6 decimales o es
            una cadena con caracteres fuera de ``[a-z0-9_]``.
    """
    if type(value) is bool:  # bool es subclase de int: se rechaza explícitamente
        msg = f"parámetro booleano no soportado: {value!r}"
        raise ValueError(msg)
    if isinstance(value, str):
        token = value.lower()
        if not _TOKEN_STR_RE.fullmatch(token):
            msg = f"parámetro {value!r} no es un identificador [a-z0-9_]"
            raise ValueError(msg)
        return token
    if isinstance(value, int):
        return f"m{-value}" if value < 0 else str(value)
    if not math.isfinite(value):
        msg = f"parámetro no finito: {value!r}"
        raise ValueError(msg)
    if round(value, MAX_PARAM_DECIMALS) != value:
        msg = f"parámetro {value!r} con más de {MAX_PARAM_DECIMALS} decimales"
        raise ValueError(msg)
    text = f"{abs(value):.{MAX_PARAM_DECIMALS}f}".rstrip("0").rstrip(".")
    if text in {"", "0"}:
        return "0"
    return ("m" if value < 0 else "") + text.replace(".", "p")


def series_id(
    prefix: str,
    name: str,
    param_tokens: Sequence[int | float | str],
    input_overrides: Sequence[str] = (),
) -> str:
    """``{prefix}{name}_{p1}_{p2}…[_{override}…]`` en minúsculas (RF-4).

    ``prefix`` es ``""`` en ``ft_indicators`` y ``"talib_"`` en ``ft_ta_lib``.

    Raises:
        ValueError: SI el prefijo, el nombre o algún token no producen un identificador
            seguro ``[a-z0-9_]``.
    """
    lowered = name.lower()
    if not _PREFIX_RE.fullmatch(prefix) or not lowered or not _ID_RE.fullmatch(lowered):
        msg = f"series_id inválido: prefijo {prefix!r} / nombre {name!r} no es un identificador"
        raise ValueError(msg)
    parts = [lowered, *(format_param(v) for v in param_tokens)]
    parts.extend(format_param(o) for o in input_overrides)
    return prefix + "_".join(parts)


def output_columns(series_id: str, outputs: Sequence[str]) -> list[str]:
    """Columnas de salida: ``[series_id]`` si la salida es única, si no ``{series_id}_{salida}``."""
    if not outputs:
        return [series_id]
    return [f"{series_id}_{out}" for out in outputs]


# ------------------------------------------------------ propios de ft_indicators

IndicatorName = Literal[
    "sma", "ema", "wma", "rsi", "macd", "bb", "atr", "stoch", "roc", "obv", "adx", "vwap"
]


class IndicatorSpec(BaseModel):
    name: IndicatorName
    params: dict[str, int | float | str] = Field(default_factory=dict)  # validado en el servicio


class IndicatorComputeRequest(BaseModel):
    datasets: list[DatasetIdentifier] = Field(min_length=1, max_length=100)
    indicators: list[IndicatorSpec] = Field(min_length=1, max_length=50)


class IndicatorPlanItem(BaseModel):
    dataset: DatasetIdentifier
    indicator_id: str
    spec: IndicatorSpec  # con defaults resueltos
    columns: list[str]


class IndicatorPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: list[IndicatorPlanItem]


class TalibEquivalentInfo(BaseModel):
    """Equivalente TA-Lib legible por máquina (validación cruzada, ``ft_ta_lib`` RF-18)."""

    function: str  # p. ej. "BBANDS"
    params: dict[str, str]  # parámetro TA-Lib -> expresión sobre params propios
    outputs: dict[str, str]  # sufijo propio -> salida TA-Lib
    compare_from: Literal["first_non_null", "stable_from"] | int  # ajuste documentado
    excluded_rows: str | None = None  # p. ej. "roc base == 0"


class IndicatorCatalogEntry(BaseModel):
    """Entrada de ``GET /indicators/catalog``."""

    name: IndicatorName
    params: dict[str, dict[str, object]]  # {param: {type, default, min, max}}
    inputs: list[str]
    outputs: list[str]  # sufijos de salida ([] si única)
    warmup: str
    stable_from: str  # fórmula legible RF-16
    f32_tolerance: dict[str, dict[str, float]]
    talib_equivalent: TalibEquivalentInfo | None  # None para vwap
    engine_version: int
    convention: str
