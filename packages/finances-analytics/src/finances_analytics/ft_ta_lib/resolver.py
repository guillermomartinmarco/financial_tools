"""Resolución y validación de una :class:`TaLibSpec` (RF-3, RF-4, RF-9; plan §3.2).

:func:`resolve` normaliza el nombre, completa los defaults en el orden de TA-Lib, convierte
``matype`` nombre↔entero, valida tipos (y rangos cuando TA-Lib los publica), mapea las
entradas (RF-3), calcula ``series_id``/columnas/dtypes y el *lookback* con los parámetros
pedidos. Si TA-Lib rechaza la combinación (*lookback* ``-1``), la ejecución de prueba aporta
su mensaje (``TA_BAD_PARAM``).

:func:`probe` ejecuta la función sobre una serie sintética de ``max(64, 2·lookback)`` filas
(``engine.probe``) para capturar ``TA_BAD_PARAM`` y confirmar los dtypes de salida.

Este módulo solo lee **metadatos** de ``talib`` (``Function.lookback``).
"""

from __future__ import annotations

import math
from collections.abc import Collection

import talib.abstract
from finances_core.indicators_store.namespaces import TALIB_PREFIX, validate_series_id
from finances_core.schemas.indicators import Namespace, format_param, output_columns, series_id
from finances_core.schemas.ta_lib import MA_TYPE_NAMES, TaLibSpec

from . import engine
from .catalog import function_table, groups
from .errors import TaLibExecutionError, TaLibValidationError
from .model import FunctionMeta, ParamMeta, ResolvedFunction, TalibParam

NAMESPACE: Namespace = "ft_ta_lib"

__all__ = ["lookup", "probe", "resolve"]


def lookup(name: str) -> FunctionMeta:
    """Metadatos de la función ``name`` (sin distinción de mayúsculas).

    Raises:
        TaLibValidationError: SI no existe (el mensaje lista los grupos válidos).
    """
    key = name.strip().upper()
    meta = function_table().get(key)
    if meta is None:
        valid = ", ".join(groups())
        msg = f"Función TA-Lib desconocida {name!r}; grupos válidos: {valid}"
        raise TaLibValidationError(msg, function=key, valid=valid)
    return meta


def _bad_param(
    meta: FunctionMeta, p: ParamMeta, value: object, expected: str
) -> TaLibValidationError:
    msg = f"{meta.name}.{p.name}={value!r} inválido: se espera {expected}"
    return TaLibValidationError(msg, function=meta.name, param=p.name, value=value, valid=expected)


def _convert(meta: FunctionMeta, p: ParamMeta, value: object) -> TalibParam:
    if isinstance(value, bool):
        raise _bad_param(meta, p, value, _expected(p))
    if p.type == "matype":
        if isinstance(value, str) and value.lower() in MA_TYPE_NAMES:
            return MA_TYPE_NAMES.index(value.lower())
        if isinstance(value, int) and 0 <= value < len(MA_TYPE_NAMES):
            return value
        raise _bad_param(meta, p, value, _expected(p))
    if p.type == "int":
        if not isinstance(value, int):
            raise _bad_param(meta, p, value, _expected(p))
        converted: TalibParam = value
    else:
        if not isinstance(value, int | float) or not math.isfinite(value):
            raise _bad_param(meta, p, value, _expected(p))
        converted = float(value)
        try:
            format_param(converted)
        except ValueError as exc:
            raise _bad_param(meta, p, value, f"{_expected(p)} ({exc})") from exc
    if (p.min is not None and converted < p.min) or (p.max is not None and converted > p.max):
        raise _bad_param(meta, p, value, _expected(p))
    return converted


def _expected(p: ParamMeta) -> str:
    if p.type == "matype":
        return f"matype: uno de {list(MA_TYPE_NAMES)} o un entero 0–{len(MA_TYPE_NAMES) - 1}"
    text = "un entero" if p.type == "int" else "un número real (máx. 6 decimales)"
    if p.min is not None or p.max is not None:
        text += f" en [{p.min}, {p.max}]"
    return text


def _params(meta: FunctionMeta, spec: TaLibSpec) -> dict[str, TalibParam]:
    unknown = [k for k in spec.params if meta.param(k) is None]
    if unknown:
        valid = ", ".join(p.name for p in meta.params) or "(ninguno)"
        msg = f"{meta.name}: parámetro inexistente {unknown[0]!r}; válidos: {valid}"
        raise TaLibValidationError(
            msg, function=meta.name, param=unknown[0], value=spec.params[unknown[0]], valid=valid
        )
    return {
        p.name: _convert(meta, p, spec.params[p.name]) if p.name in spec.params else p.default
        for p in meta.params
    }


def _inputs(
    meta: FunctionMeta, spec: TaLibSpec, available: Collection[str] | None
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    for key in spec.inputs:
        info = meta.input(key)
        if info is None:
            singles = [i.name for i in meta.inputs if i.single] or ["(ninguna)"]
            msg = f"{meta.name}: entrada inexistente {key!r}; redefinibles: {', '.join(singles)}"
            raise TaLibValidationError(msg, function=meta.name, param=key, valid=", ".join(singles))
        if not info.single:
            msg = (
                f"{meta.name}: la entrada compuesta {key!r} no se puede redefinir "
                f"(usa siempre {list(info.columns)})"
            )
            raise TaLibValidationError(msg, function=meta.name, param=key)
    inputs: dict[str, tuple[str, ...]] = {}
    overrides: dict[str, str] = {}
    for info in meta.inputs:
        column = spec.inputs.get(info.name)
        if column is not None and column != info.columns[0]:
            overrides[info.name] = column
            inputs[info.name] = (column,)
        else:
            inputs[info.name] = info.columns
    if available is not None:
        for name, cols in inputs.items():
            missing = [c for c in cols if c not in available]
            if missing:
                msg = f"{meta.name}: la entrada {name!r} usa {missing}, ausente en el dataset"
                raise TaLibValidationError(msg, function=meta.name, param=name, value=missing[0])
    return inputs, overrides


def _lookback(meta: FunctionMeta, params: dict[str, TalibParam]) -> int:
    function = talib.abstract.Function(meta.name)
    function.set_parameters(dict(params))
    return int(function.lookback)


def resolve(spec: TaLibSpec, available_columns: Collection[str] | None = None) -> ResolvedFunction:
    """Valida ``spec`` y la completa (``available_columns``: columnas del dataset, si se conoce).

    Raises:
        TaLibValidationError: SI la función no existe o no está soportada, un parámetro no
            existe, tiene tipo erróneo o TA-Lib lo rechaza (``TA_BAD_PARAM``), o una entrada
            es compuesta, inexistente o usa una columna ausente.
    """
    meta = lookup(spec.function)
    if not meta.supported:
        msg = f"Función {meta.name} no soportada: {meta.unsupported_reason}"
        raise TaLibValidationError(msg, function=meta.name, valid=str(meta.unsupported_reason))
    params = _params(meta, spec)
    inputs, overrides = _inputs(meta, spec, available_columns)
    tokens: list[int | float | str] = [
        MA_TYPE_NAMES[int(params[p.name])] if p.type == "matype" else params[p.name]
        for p in meta.params
    ]
    override_tokens = [t for name, col in overrides.items() for t in (name, col)]
    sid = series_id(TALIB_PREFIX, meta.name, tokens, override_tokens)
    names = [o.name for o in meta.outputs]
    columns = dict(zip(names, output_columns(sid, names if len(names) > 1 else []), strict=True))
    for column in (sid, *columns.values()):
        validate_series_id(NAMESPACE, column)
    resolved = ResolvedFunction(
        meta=meta,
        params=params,
        inputs=inputs,
        overrides=overrides,
        series_id=sid,
        columns=columns,
        lookback=_lookback(meta, params),
    )
    if resolved.lookback < 0:  # TA-Lib rechaza la combinación: su mensaje viene del probe
        probe(resolved)
        msg = f"{meta.name}: TA-Lib rechaza los parámetros {params} (TA_BAD_PARAM)"
        raise TaLibValidationError(msg, function=meta.name)
    return resolved


def probe(resolved: ResolvedFunction) -> ResolvedFunction:
    """Ejecución de prueba sobre una serie sintética (RF-4): captura ``TA_BAD_PARAM`` y
    confirma que los dtypes de salida coinciden con el catálogo.

    Raises:
        TaLibValidationError: SI TA-Lib rechaza la ejecución (mensaje de TA-Lib).
    """
    try:
        integer = engine.probe(resolved)
    except TaLibExecutionError as exc:
        raise TaLibValidationError(str(exc), function=resolved.name) from exc
    expected = [o.kind != "real" for o in resolved.meta.outputs]
    if integer != expected:
        msg = f"{resolved.name}: tipos de salida {integer} distintos del catálogo {expected}"
        raise TaLibValidationError(msg, function=resolved.name)
    return resolved
