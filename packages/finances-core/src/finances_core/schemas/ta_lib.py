"""Contratos propios de ``ft_ta_lib`` (plan de ``ft_ta_lib`` §1.1).

Reutiliza los contratos genéricos de :mod:`finances_core.schemas.indicators` (``Stability``,
``StoreEntry``, ``StoreManifest``, ``ComputeProgress``, ``ComputeItemResult``,
``ComputeResult``, ``DispatchResponse``, ``SeriesExportRequest`` y la nomenclatura
``format_param``/``series_id``/``output_columns``). Aquí solo viven los propios:
petición, plan y catálogo de TA-Lib.

Este módulo **no** importa ``talib`` (Art. 4 / RFC-0002): los nombres de tipos de media
(:data:`MA_TYPE_NAMES`) replican ``talib.MA_Type`` y un test de ``ft_ta_lib`` verifica que
coinciden con la versión instalada.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import (
    BaseModel,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import ColumnDtype, PriceSource

__all__ = [
    "MA_TYPE_NAMES",
    "TaLibCatalog",
    "TaLibCatalogEntry",
    "TaLibComputeRequest",
    "TaLibInputColumn",
    "TaLibOutputInfo",
    "TaLibOutputKind",
    "TaLibParamInfo",
    "TaLibPlan",
    "TaLibPlanItem",
    "TaLibSpec",
    "TaLibStabilityRule",
    "TaLibUnsupportedReason",
]

#: Tipo de salida de una función TA-Lib (RF-7).
TaLibOutputKind = Literal["real", "pattern", "category", "index"]
#: Columna que puede redefinir una entrada de una serie (RF-3).
TaLibInputColumn = PriceSource | Literal["volume"]
TaLibStabilityRule = Literal["first_non_null", "unstable_period_unknown"]
TaLibUnsupportedReason = Literal["unavailable_input", "non_causal"]

#: Nombres de los tipos de media móvil; la posición es el entero de ``talib.MA_Type``.
#: TA-Lib C 0.8.1 publica 14 tipos (0–13): los 9 clásicos (``sma``…``t3``) más ``hma``,
#: ``disabled`` (identidad), ``default`` (el de la propia función), ``zlema`` y ``rma``.
MA_TYPE_NAMES: tuple[str, ...] = (
    "sma",
    "ema",
    "wma",
    "dema",
    "tema",
    "trima",
    "kama",
    "mama",
    "t3",
    "hma",
    "disabled",
    "default",
    "zlema",
    "rma",
)

_FUNCTION_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class TaLibSpec(BaseModel):
    """Una función de TA-Lib pedida (RF-5); los defaults los completa el resolutor."""

    function: str  # nombre TA-Lib, sin distinción de mayúsculas (normalizado a MAYÚSCULAS)
    # str solo para *matype; estricto: un booleano JSON nunca se convierte en 0/1.
    params: dict[str, StrictInt | StrictFloat | StrictStr] = Field(default_factory=dict)
    inputs: dict[str, TaLibInputColumn] = Field(default_factory=dict)  # solo price/price0/price1

    @field_validator("function")
    @classmethod
    def _normalize_function(cls, value: str) -> str:
        name = value.strip().upper()
        if not _FUNCTION_RE.fullmatch(name):
            msg = f"nombre de función {value!r} no es un identificador [A-Za-z_][A-Za-z0-9_]*"
            raise ValueError(msg)
        return name


class TaLibComputeRequest(BaseModel):
    """Petición canónica de ``POST /ta-lib/compute`` y de la CLI (RF-5)."""

    datasets: list[DatasetIdentifier] = Field(min_length=1, max_length=100)
    functions: list[TaLibSpec] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)  # ≤ 200 funciones tras expandir

    @model_validator(mode="after")
    def _functions_or_groups(self) -> Self:
        if not self.functions and not self.groups:
            msg = "la petición necesita al menos una entrada en functions o groups"
            raise ValueError(msg)
        return self


class TaLibPlanItem(BaseModel):
    dataset: DatasetIdentifier
    series_id: str
    spec: TaLibSpec  # con defaults resueltos
    columns: dict[str, ColumnDtype]  # columna -> dtype persistido
    lookback: int


class TaLibPlan(BaseModel):
    task_id: str
    created_at_ms: int
    items: list[TaLibPlanItem]


class TaLibParamInfo(BaseModel):
    name: str
    type: Literal["int", "float", "matype"]
    default: int | float | str  # matype: nombre de MA_TYPE_NAMES
    min: float | None = None
    max: float | None = None


class TaLibOutputInfo(BaseModel):
    name: str  # nombre TA-Lib (p. ej. "upperband")
    kind: TaLibOutputKind
    dtype: ColumnDtype


class TaLibCatalogEntry(BaseModel):
    """Entrada de ``GET /ta-lib/catalog`` (RF-2)."""

    function: str
    group: str
    display_name: str
    flags: list[str]  # function_flags de TA-Lib
    inputs: dict[str, list[str]]  # nombre de entrada TA-Lib -> columnas por defecto
    params: list[TaLibParamInfo]
    outputs: list[TaLibOutputInfo]
    default_lookback: int
    stability_rule: TaLibStabilityRule
    supported: bool
    unsupported_reason: TaLibUnsupportedReason | None = None


class TaLibCatalog(BaseModel):
    talib_version: str  # wrapper, p. ej. "0.8.2"
    ta_lib_c_version: str  # p. ej. "0.8.1"
    groups: dict[str, list[str]]
    functions: list[TaLibCatalogEntry]
