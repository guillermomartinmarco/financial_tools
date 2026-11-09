"""Contrato de estrategia (RF-16, plan §1.3, ADR-B11).

Una estrategia es una subclase concreta de :class:`BaseStrategy` con métodos estáticos
puros: ``warmup(params)`` y ``signals(candles, params)``. ``Params`` (Pydantic con
``extra="forbid"``) es la fuente única de tipos, rangos y relaciones. El contrato solo
expresa señales largo/plano sin estado de ejecución (ADR-B09).
"""

from __future__ import annotations

import inspect
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Generic, Literal, NamedTuple, TypeVar

import polars as pl
from pydantic import BaseModel, ConfigDict, ValidationError

#: ``kind`` válido (RF-16): igual al nombre de la carpeta.
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
#: SemVer 2.0.0 (``MAJOR.MINOR.PATCH`` con prerelease/build opcionales).
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

MetricName = Literal[
    "total_return", "cagr", "max_drawdown", "sharpe", "sortino", "win_rate", "exposure"
]

P = TypeVar("P", bound=BaseModel)


class Signals(NamedTuple):
    """Señales al cierre de cada barra ``t`` (RF-2, Enmienda G2b): cuatro ``pl.Series``
    ``Boolean`` de la misma longitud. ``entries``/``exits`` son el lado largo;
    ``entries_short``/``exits_short`` el lado corto. **Siempre poblados** (incluso con todo
    ``False`` cuando no hay cortos): ningún motor necesita distinguir un campo ausente.
    """

    entries: pl.Series
    exits: pl.Series
    entries_short: pl.Series
    exits_short: pl.Series


class MetricsTarget(BaseModel):
    """Objetivo declarado para una futura optimización (RF-16); solo informativo."""

    model_config = ConfigDict(extra="forbid")

    metric: MetricName
    direction: Literal["max", "min"]


class StrategyDefinitionError(TypeError):
    """La clase no cumple el contrato :class:`BaseStrategy` (motivo legible)."""


class BaseStrategy(ABC, Generic[P]):
    """Clase base de toda estrategia (integrada o local).

    Las subclases definen los atributos de clase y los dos métodos estáticos; el núcleo
    nunca necesita instancias, pero instanciar una definición incompleta falla.
    """

    kind: ClassVar[str]  # ^[a-z][a-z0-9_]{2,39}$ == nombre de carpeta
    version: ClassVar[str]  # SemVer de la lógica
    description: ClassVar[str]  # una línea
    Params: ClassVar[type[BaseModel]]  # extra="forbid"; fuente única de tipos y rangos
    default_params: ClassVar[Mapping[str, int | float | bool | str]]  # DEBE validar
    metrics_target: ClassVar[MetricsTarget | None] = None
    #: Parámetros adicionales para la verificación de autor (plan §3.8); opcional.
    check_params: ClassVar[Sequence[Mapping[str, int | float | bool | str]]] = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> BaseStrategy[P]:
        validate_strategy_class(cls)
        return super().__new__(cls)

    @staticmethod
    @abstractmethod
    def warmup(params: P) -> int:
        """Primer índice ``t`` con señal posible; el núcleo fuerza ``false`` antes."""

    @staticmethod
    @abstractmethod
    def signals(candles: pl.DataFrame, params: P) -> pl.Series:
        """Posición-objetivo ``Int8 {-1, 0, +1}`` al cierre de cada barra ``t``.
        (+1 largo, 0 plano, -1 corto). Pura y determinista sobre las velas de RF-4.
        """


def _require_str(cls: type, name: str) -> str:
    value = getattr(cls, name, None)
    if not isinstance(value, str):
        msg = f"{cls.__name__}.{name} debe ser un str"
        raise StrategyDefinitionError(msg)
    return value


def _check_params_model(cls: type) -> type[BaseModel]:
    params = getattr(cls, "Params", None)
    if not (isinstance(params, type) and issubclass(params, BaseModel)):
        msg = f"{cls.__name__}.Params debe ser una subclase de pydantic.BaseModel"
        raise StrategyDefinitionError(msg)
    if params.model_config.get("extra") != "forbid":
        msg = f"{cls.__name__}.Params debe declarar model_config = ConfigDict(extra='forbid')"
        raise StrategyDefinitionError(msg)
    return params


def _validate_params(cls: type, params: type[BaseModel], name: str, values: object) -> None:
    if not isinstance(values, Mapping):
        msg = f"{cls.__name__}.{name} debe ser un mapeo"
        raise StrategyDefinitionError(msg)
    try:
        params.model_validate(dict(values))
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in err["loc"]) or "params" for err in exc.errors())
        msg = f"{cls.__name__}.{name} no valida contra Params ({fields})"
        raise StrategyDefinitionError(msg) from None


def validate_strategy_class(cls: type) -> None:
    """Comprueba que ``cls`` sea una estrategia concreta y completa (RF-16, RF-18).

    Raises:
        StrategyDefinitionError: SI no es subclase de :class:`BaseStrategy`, le falta
            ``warmup``/``signals`` (abstracta), ``kind``/``version``/``description`` no
            cumplen su formato, ``Params`` no es un modelo con ``extra="forbid"``,
            ``default_params``/``check_params`` no validan o ``metrics_target`` no es un
            :class:`MetricsTarget`.
    """
    if not (isinstance(cls, type) and issubclass(cls, BaseStrategy)):
        msg = f"{getattr(cls, '__name__', cls)!r} no es una subclase de BaseStrategy"
        raise StrategyDefinitionError(msg)
    if inspect.isabstract(cls):
        missing = ", ".join(sorted(getattr(cls, "__abstractmethods__", ())))
        msg = f"{cls.__name__} no implementa: {missing}"
        raise StrategyDefinitionError(msg)
    if not KIND_RE.fullmatch(_require_str(cls, "kind")):
        msg = f"{cls.__name__}.kind {cls.kind!r} no cumple {KIND_RE.pattern}"
        raise StrategyDefinitionError(msg)
    if not SEMVER_RE.fullmatch(_require_str(cls, "version")):
        msg = f"{cls.__name__}.version {cls.version!r} no es SemVer (MAJOR.MINOR.PATCH)"
        raise StrategyDefinitionError(msg)
    description = _require_str(cls, "description")
    if not description.strip() or "\n" in description:
        msg = f"{cls.__name__}.description debe ser una única línea no vacía"
        raise StrategyDefinitionError(msg)
    params = _check_params_model(cls)
    _validate_params(cls, params, "default_params", getattr(cls, "default_params", None))
    for index, extra in enumerate(getattr(cls, "check_params", ())):
        _validate_params(cls, params, f"check_params[{index}]", extra)
    target = getattr(cls, "metrics_target", None)
    if target is not None and not isinstance(target, MetricsTarget):
        msg = f"{cls.__name__}.metrics_target debe ser un MetricsTarget o None"
        raise StrategyDefinitionError(msg)
