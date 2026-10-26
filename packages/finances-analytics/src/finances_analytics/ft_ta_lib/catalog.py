"""Catálogo dinámico de TA-Lib (RF-2, RF-7, RF-17; plan §3.1, ADR-T02).

Se construye **en tiempo de ejecución** desde ``talib.get_function_groups()`` y
``talib.abstract.Function(name)`` (``info``, ``parameters``, ``lookback``): una función nueva
de una versión de TA-Lib aparece sin cambios de código.

- **Tipo de parámetro**: ``matype`` si el nombre termina en ``matype``; si no, por el tipo
  del default (``int``/``float``). **Rango**: ta-lib 0.8.2 no expone el rango de
  ``TA_GetOptInputParameterInfo`` (su ``_ta_getOptInputParameterInfo`` privado solo devuelve
  nombre, tipo, ayuda y default), así que ``min``/``max`` son ``None`` y la validación recae
  en la ejecución de prueba (``resolver.probe``, ``TA_BAD_PARAM``).
- **Tipo de salida**: ``double`` → ``real``; entera en *Pattern Recognition* → ``pattern``;
  entera en :data:`INDEX_FUNCTIONS` → ``index``; otra entera → ``category``. El tipo entero se
  detecta por el ``dtype`` del array de salida en la ejecución de prueba sobre la serie
  sintética (``engine.integer_outputs``).
- **Soporte**: entrada fuera de OHLCV (``periods`` de ``MAVP``) → ``unavailable_input``;
  :data:`NON_CAUSAL` → ``non_causal``.
- **Estabilidad**: *flag* ``"Function has an unstable period"`` → ``unstable_period_unknown``;
  si no → ``first_non_null``.

Este módulo solo lee **metadatos** de ``talib``; el cálculo vive en ``engine.py``.
"""

from __future__ import annotations

import functools
from collections.abc import Collection, Mapping
from typing import Any

import talib
import talib.abstract  # registra el submódulo ``talib.abstract``
from finances_core.schemas.indicators import ColumnDtype
from finances_core.schemas.ta_lib import (
    TaLibCatalog,
    TaLibOutputKind,
    TaLibStabilityRule,
    TaLibUnsupportedReason,
)

from . import engine
from .model import (
    OHLCV_COLUMNS,
    UNSTABLE_FLAG,
    FunctionMeta,
    InputMeta,
    OutputMeta,
    ParamMeta,
    ParamType,
)

PATTERN_GROUP = "Pattern Recognition"
#: Funciones cuya salida entera es la posición de una fila (RF-7) → ``open_time_ms``.
INDEX_FUNCTIONS = frozenset({"MAXINDEX", "MININDEX", "MINMAXINDEX"})
#: Funciones que fallan la propiedad *sin anticipación* (T3.2). Ninguna con ta-lib 0.8.2 /
#: TA-Lib C 0.8.1: ``test_properties.py`` recorre todo el catálogo soportado y lo documenta.
NON_CAUSAL: frozenset[str] = frozenset()

_KIND_DTYPE: dict[TaLibOutputKind, ColumnDtype] = {
    "real": "Float32",
    "pattern": "Int16",
    "category": "Int16",
    "index": "Int64",
}

__all__ = [
    "INDEX_FUNCTIONS",
    "NON_CAUSAL",
    "PATTERN_GROUP",
    "build_catalog",
    "build_catalog_from",
    "build_function_table",
    "c_version",
    "function_table",
    "groups",
]


def c_version(lib: Any = talib) -> str:
    """Versión de TA-Lib C (p. ej. ``"0.8.1"`` de ``b"0.8.1 (Oct 12 2026 …)"``)."""
    raw = lib.__ta_version__
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    return text.split()[0]


def _param_type(name: str, default: object) -> ParamType:
    if name.endswith("matype"):
        return "matype"
    return "int" if type(default) is int else "float"


def _inputs(raw: Mapping[str, Any]) -> tuple[InputMeta, ...]:
    out: list[InputMeta] = []
    for name, cols in raw.items():
        single = isinstance(cols, str)
        columns = (cols,) if single else tuple(str(c) for c in cols)
        out.append(InputMeta(name=str(name), columns=columns, single=single))
    return tuple(out)


def _output_kind(name: str, group: str, *, integer: bool) -> TaLibOutputKind:
    if not integer:
        return "real"
    if group == PATTERN_GROUP:
        return "pattern"
    if name in INDEX_FUNCTIONS:
        return "index"
    return "category"


def _function_meta(lib: Any, name: str, group: str, non_causal: Collection[str]) -> FunctionMeta:
    function = lib.abstract.Function(name)
    info: Mapping[str, Any] = function.info
    inputs = _inputs(info["input_names"])
    params = tuple(
        ParamMeta(name=str(p), type=_param_type(str(p), d), default=d)
        for p, d in function.parameters.items()
    )
    lookback = int(function.lookback)
    columns = [c for i in inputs for c in i.columns]
    integer = engine.integer_outputs(
        function, columns, max(engine.MIN_PROBE_ROWS, 2 * max(lookback, 0))
    )
    outputs = tuple(
        OutputMeta(name=str(out), kind=kind, dtype=_KIND_DTYPE[kind])
        for out, is_int in zip(info["output_names"], integer, strict=True)
        for kind in (_output_kind(name, group, integer=is_int),)
    )
    flags = tuple(str(f) for f in info.get("function_flags") or ())
    reason: TaLibUnsupportedReason | None = None
    if any(c not in OHLCV_COLUMNS for c in columns):
        reason = "unavailable_input"
    elif name in non_causal:
        reason = "non_causal"
    rule: TaLibStabilityRule = (
        "unstable_period_unknown" if UNSTABLE_FLAG in flags else "first_non_null"
    )
    return FunctionMeta(
        name=name,
        group=group,
        display_name=str(info.get("display_name") or name),
        flags=flags,
        inputs=inputs,
        params=params,
        outputs=outputs,
        default_lookback=lookback,
        stability_rule=rule,
        supported=reason is None,
        unsupported_reason=reason,
    )


def build_function_table(
    lib: Any = talib, *, non_causal: Collection[str] = NON_CAUSAL
) -> dict[str, FunctionMeta]:
    """Metadatos de cada función de ``lib`` (por nombre, en el orden de sus grupos)."""
    table: dict[str, FunctionMeta] = {}
    for group, names in lib.get_function_groups().items():
        for name in names:
            table[str(name)] = _function_meta(lib, str(name), str(group), non_causal)
    return table


def build_catalog_from(
    lib: Any = talib, *, non_causal: Collection[str] = NON_CAUSAL
) -> TaLibCatalog:
    """:class:`TaLibCatalog` de ``lib`` (``talib`` o un módulo compatible)."""
    table = build_function_table(lib, non_causal=non_causal)
    return TaLibCatalog(
        talib_version=str(lib.__version__),
        ta_lib_c_version=c_version(lib),
        groups={str(g): [str(n) for n in ns] for g, ns in lib.get_function_groups().items()},
        functions=[meta.entry() for meta in table.values()],
    )


@functools.cache
def function_table() -> dict[str, FunctionMeta]:
    """Tabla de la versión instalada de TA-Lib (cacheada; no mutar)."""
    return build_function_table(talib)


@functools.cache
def build_catalog() -> TaLibCatalog:
    """Catálogo de la versión instalada de TA-Lib (cacheado)."""
    table = function_table()
    return TaLibCatalog(
        talib_version=str(talib.__version__),
        ta_lib_c_version=c_version(talib),
        groups=groups(),
        functions=[meta.entry() for meta in table.values()],
    )


def groups() -> dict[str, list[str]]:
    """Grupos de TA-Lib → funciones."""
    return {str(g): [str(n) for n in ns] for g, ns in talib.get_function_groups().items()}
