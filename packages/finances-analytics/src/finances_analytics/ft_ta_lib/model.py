"""Modelos internos de ``ft_ta_lib`` (sin ``talib``): metadatos de catálogo y función resuelta.

- :class:`FunctionMeta`: lo que el catálogo sabe de una función de TA-Lib (plan §3.1).
- :class:`ResolvedFunction`: una :class:`TaLibSpec` validada con todos sus parámetros en el
  orden de TA-Lib, entradas mapeadas a columnas, ``series_id``, columnas y dtypes (plan §3.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from finances_core.schemas.indicators import ColumnDtype
from finances_core.schemas.ta_lib import (
    MA_TYPE_NAMES,
    TaLibCatalogEntry,
    TaLibOutputInfo,
    TaLibOutputKind,
    TaLibParamInfo,
    TaLibSpec,
    TaLibStabilityRule,
    TaLibUnsupportedReason,
)

ParamType = Literal["int", "float", "matype"]
#: Valor de parámetro tal como lo recibe TA-Lib (``matype`` como entero).
TalibParam = int | float

#: Columnas canónicas a las que se mapean las entradas de TA-Lib (RF-3).
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
#: Flag de TA-Lib que marca las funciones con *unstable period* (RF-17).
UNSTABLE_FLAG = "Function has an unstable period"


@dataclass(frozen=True)
class ParamMeta:
    """Parámetro opcional de TA-Lib (``default`` crudo: ``matype`` como entero)."""

    name: str
    type: ParamType
    default: TalibParam
    min: float | None = None
    max: float | None = None

    def public_default(self) -> int | float | str:
        """Default publicado: ``matype`` por nombre (``sma``, ``ema``, …)."""
        if self.type == "matype":
            return MA_TYPE_NAMES[int(self.default)]
        return self.default

    def info(self) -> TaLibParamInfo:
        return TaLibParamInfo(
            name=self.name,
            type=self.type,
            default=self.public_default(),
            min=self.min,
            max=self.max,
        )


@dataclass(frozen=True)
class OutputMeta:
    name: str
    kind: TaLibOutputKind
    dtype: ColumnDtype


@dataclass(frozen=True)
class InputMeta:
    """Entrada de TA-Lib: ``single`` (``price``/``price0``/``price1``) o compuesta (``prices``)."""

    name: str
    columns: tuple[str, ...]  # columnas por defecto
    single: bool


@dataclass(frozen=True)
class FunctionMeta:
    """Metadatos de una función del catálogo (RF-2)."""

    name: str
    group: str
    display_name: str
    flags: tuple[str, ...]
    inputs: tuple[InputMeta, ...]
    params: tuple[ParamMeta, ...]
    outputs: tuple[OutputMeta, ...]
    default_lookback: int
    stability_rule: TaLibStabilityRule
    supported: bool
    unsupported_reason: TaLibUnsupportedReason | None = None

    def param(self, name: str) -> ParamMeta | None:
        return next((p for p in self.params if p.name == name), None)

    def input(self, name: str) -> InputMeta | None:
        return next((i for i in self.inputs if i.name == name), None)

    def entry(self) -> TaLibCatalogEntry:
        """Forma publicada en ``GET /ta-lib/catalog``."""
        return TaLibCatalogEntry(
            function=self.name,
            group=self.group,
            display_name=self.display_name,
            flags=list(self.flags),
            inputs={i.name: list(i.columns) for i in self.inputs},
            params=[p.info() for p in self.params],
            outputs=[
                TaLibOutputInfo(name=o.name, kind=o.kind, dtype=o.dtype) for o in self.outputs
            ],
            default_lookback=self.default_lookback,
            stability_rule=self.stability_rule,
            supported=self.supported,
            unsupported_reason=self.unsupported_reason,
        )


@dataclass(frozen=True)
class ResolvedFunction:
    """Función validada y lista para el motor (plan §3.2)."""

    meta: FunctionMeta
    params: dict[str, TalibParam]  # orden de TA-Lib; matype como entero
    inputs: dict[str, tuple[str, ...]]  # entrada TA-Lib -> columnas fuente (tras redefinir)
    overrides: dict[str, str]  # entradas redefinidas (≠ default) -> columna
    series_id: str
    columns: dict[str, str]  # salida TA-Lib -> columna persistida
    lookback: int

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def source_columns(self) -> tuple[str, ...]:
        """Columnas fuente necesarias, sin repetir y en orden de entrada."""
        return tuple(dict.fromkeys(c for cols in self.inputs.values() for c in cols))

    @property
    def dtypes(self) -> dict[str, ColumnDtype]:
        """Columna persistida -> dtype (RF-7)."""
        return {self.columns[o.name]: o.dtype for o in self.meta.outputs}

    def output(self, name: str) -> OutputMeta:
        return next(o for o in self.meta.outputs if o.name == name)

    def spec(self) -> TaLibSpec:
        """Spec canónica con defaults resueltos (``matype`` por nombre)."""
        params: dict[str, int | float | str] = {}
        for p in self.meta.params:
            value = self.params[p.name]
            params[p.name] = MA_TYPE_NAMES[int(value)] if p.type == "matype" else value
        return TaLibSpec.model_validate(
            {"function": self.name, "params": params, "inputs": dict(self.overrides)}
        )


__all__ = [
    "OHLCV_COLUMNS",
    "UNSTABLE_FLAG",
    "FunctionMeta",
    "InputMeta",
    "OutputMeta",
    "ParamMeta",
    "ParamType",
    "ResolvedFunction",
    "TalibParam",
]
