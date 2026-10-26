"""T2.1–T2.2: catálogo dinámico de TA-Lib (RF-2, RF-7, RF-17; plan §3.1).

Vía de metadatos de parámetros (T2.2): ``talib.abstract.Function(name).parameters`` da
nombre y default (tipo por el default; ``matype`` por el sufijo del nombre). ta-lib 0.8.2
**no** expone el rango de ``TA_GetOptInputParameterInfo``: su ``_ta_getOptInputParameterInfo``
(privado) solo devuelve ``name``/``display_name``/``type``/``help``/``default_value``, así que
``min``/``max`` se publican como ``None`` y la validación de rango recae en la ejecución de
prueba (``resolver.probe``, ``TA_BAD_PARAM``).
"""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import talib
from finances_core.schemas.ta_lib import MA_TYPE_NAMES, TaLibCatalog, TaLibCatalogEntry
from talib import abstract

from finances_analytics.ft_ta_lib import catalog

PATTERN_GROUP = "Pattern Recognition"


@pytest.fixture(scope="module")
def built() -> TaLibCatalog:
    return catalog.build_catalog()


def _entry(cat: TaLibCatalog, name: str) -> TaLibCatalogEntry:
    return next(e for e in cat.functions if e.function == name)


# --- T2.1 ------------------------------------------------------------------------------


def test_catalog_contains_every_talib_function_with_its_group(built: TaLibCatalog) -> None:
    names = [e.function for e in built.functions]
    assert sorted(names) == sorted(talib.get_functions())
    assert len(names) == len(set(names))
    groups = talib.get_function_groups()
    assert built.groups == {g: list(fs) for g, fs in groups.items()}
    for group, functions in groups.items():
        for name in functions:
            assert _entry(built, name).group == group


def test_pattern_recognition_has_61_functions(built: TaLibCatalog) -> None:
    patterns = [e for e in built.functions if e.group == PATTERN_GROUP]
    assert len(patterns) == 61
    assert all(e.function.startswith("CDL") for e in patterns)
    assert all(e.supported for e in patterns)


def test_bbands_has_three_outputs_and_matype(built: TaLibCatalog) -> None:
    bb = _entry(built, "BBANDS")
    assert [o.name for o in bb.outputs] == ["upperband", "middleband", "lowerband"]
    params = {p.name: p for p in bb.params}
    assert list(params) == ["timeperiod", "nbdevup", "nbdevdn", "matype"]
    assert params["matype"].type == "matype"
    assert params["matype"].default == "sma"
    assert params["nbdevup"].type == "float"
    assert bb.inputs == {"price": ["close"]}
    assert bb.display_name == "Bollinger Bands"
    assert bb.default_lookback == abstract.Function("BBANDS").lookback
    assert bb.supported
    assert bb.unsupported_reason is None


def test_mavp_is_unsupported_for_unavailable_input(built: TaLibCatalog) -> None:
    mavp = _entry(built, "MAVP")
    assert mavp.supported is False
    assert mavp.unsupported_reason == "unavailable_input"
    assert "periods" in mavp.inputs


def test_only_unmappable_inputs_are_unsupported(built: TaLibCatalog) -> None:
    unsupported = {e.function: e.unsupported_reason for e in built.functions if not e.supported}
    assert unsupported == {"MAVP": "unavailable_input"}


def test_composite_inputs_keep_their_default_columns(built: TaLibCatalog) -> None:
    assert _entry(built, "STOCH").inputs == {"prices": ["high", "low", "close"]}
    assert _entry(built, "OBV").inputs == {"price": ["close"], "prices": ["volume"]}
    assert _entry(built, "BETA").inputs == {"price0": ["high"], "price1": ["low"]}


def test_versions_of_wrapper_and_c_library_are_present(built: TaLibCatalog) -> None:
    assert built.talib_version == talib.__version__
    raw = talib.__ta_version__
    c_version = raw.decode() if isinstance(raw, bytes) else str(raw)
    assert built.ta_lib_c_version == c_version.split()[0]
    assert built.ta_lib_c_version.count(".") == 2


def test_flags_and_stability_rule(built: TaLibCatalog) -> None:
    rsi = _entry(built, "RSI")
    assert "Function has an unstable period" in rsi.flags
    assert rsi.stability_rule == "unstable_period_unknown"
    assert _entry(built, "SMA").stability_rule == "first_non_null"
    assert "Output is a candlestick" in _entry(built, "CDLDOJI").flags


def test_build_catalog_is_cached() -> None:
    assert catalog.build_catalog() is catalog.build_catalog()


class _FakeFunction:
    """Función ``talib.abstract`` simulada (solo lo que lee el catálogo)."""

    def __init__(self, name: str) -> None:
        self.info: dict[str, Any] = {
            "name": name,
            "group": "Custom Indicators",
            "display_name": "Fake Oscillator",
            "function_flags": ["Function has a streaming API"],
            "input_names": OrderedDict({"price": "close"}),
            "parameters": OrderedDict({"timeperiod": 7, "matype": 1}),
            "output_flags": OrderedDict({"real": ["Line"], "signal": ["Line"]}),
            "output_names": ["real", "signal"],
        }
        self.parameters = self.info["parameters"]
        self.output_names = self.info["output_names"]
        self.lookback = 6

    def __call__(self, inputs: dict[str, np.ndarray], **_params: object) -> list[np.ndarray]:
        n = len(inputs["close"])
        return [np.zeros(n), np.zeros(n, dtype=np.int32)]


def test_new_function_of_a_simulated_talib_appears_without_code_changes() -> None:
    groups = {**talib.get_function_groups(), "Custom Indicators": ["FAKEOSC"]}

    def function(name: str) -> object:
        return _FakeFunction(name) if name == "FAKEOSC" else abstract.Function(name)

    fake = SimpleNamespace(
        __version__="9.9.9",
        __ta_version__=b"9.9.1 (Jan  1 2030 00:00:00)",
        get_function_groups=lambda: groups,
        abstract=SimpleNamespace(Function=function),
    )
    cat = catalog.build_catalog_from(fake)
    entry = _entry(cat, "FAKEOSC")
    assert cat.talib_version == "9.9.9"
    assert cat.ta_lib_c_version == "9.9.1"
    assert entry.group == "Custom Indicators"
    assert entry.supported
    assert [(o.name, o.kind, o.dtype) for o in entry.outputs] == [
        ("real", "real", "Float32"),
        ("signal", "category", "Int16"),
    ]
    assert [(p.name, p.type, p.default) for p in entry.params] == [
        ("timeperiod", "int", 7),
        ("matype", "matype", "ema"),
    ]
    assert entry.default_lookback == 6
    assert cat.groups["Custom Indicators"] == ["FAKEOSC"]


def test_non_causal_functions_are_marked_unsupported() -> None:
    table = catalog.build_function_table(talib, non_causal=frozenset({"SMA"}))
    assert table["SMA"].supported is False
    assert table["SMA"].unsupported_reason == "non_causal"
    assert table["EMA"].supported


# --- T2.2 ------------------------------------------------------------------------------


def test_rsi_timeperiod_metadata(built: TaLibCatalog) -> None:
    (param,) = _entry(built, "RSI").params
    assert (param.name, param.type, param.default) == ("timeperiod", "int", 14)
    # ta-lib 0.8.2 no expone el rango (ver docstring del módulo): validación por probe.
    assert param.min is None
    assert param.max is None


@pytest.mark.parametrize(
    ("name", "output", "kind", "dtype"),
    [
        ("CDLENGULFING", "integer", "pattern", "Int16"),
        ("CDLHIKKAKE", "integer", "pattern", "Int16"),
        ("HT_TRENDMODE", "integer", "category", "Int16"),
        ("MINMAXINDEX", "minidx", "index", "Int64"),
        ("MINMAXINDEX", "maxidx", "index", "Int64"),
        ("MAXINDEX", "integer", "index", "Int64"),
        ("MININDEX", "integer", "index", "Int64"),
        ("RSI", "real", "real", "Float32"),
        ("BBANDS", "upperband", "real", "Float32"),
        ("HT_DCPERIOD", "real", "real", "Float32"),
    ],
)
def test_output_kinds(built: TaLibCatalog, name: str, output: str, kind: str, dtype: str) -> None:
    out = next(o for o in _entry(built, name).outputs if o.name == output)
    assert (out.kind, out.dtype) == (kind, dtype)


def test_every_output_kind_is_consistent(built: TaLibCatalog) -> None:
    for entry in built.functions:
        for out in entry.outputs:
            expected = {"real": "Float32", "pattern": "Int16", "category": "Int16"}
            assert out.dtype == expected.get(out.kind, "Int64"), (entry.function, out)
            if out.kind == "pattern":
                assert entry.group == PATTERN_GROUP
            if out.kind == "index":
                assert entry.function in catalog.INDEX_FUNCTIONS


def test_matype_defaults_are_published_by_name(built: TaLibCatalog) -> None:
    matypes = [(e.function, p) for e in built.functions for p in e.params if p.type == "matype"]
    assert matypes
    for _function, param in matypes:
        assert param.default in MA_TYPE_NAMES


def test_ma_type_names_mirror_talib_enum() -> None:
    """``MA_TYPE_NAMES`` (núcleo, sin ``talib``) replica ``talib.MA_Type`` instalado."""
    enum = {getattr(talib.MA_Type, a): a.lower() for a in dir(talib.MA_Type) if a.isupper()}
    assert dict(enumerate(MA_TYPE_NAMES)) == enum


def test_param_types_follow_defaults(built: TaLibCatalog) -> None:
    for entry in built.functions:
        for p in entry.params:
            if p.name.endswith("matype"):
                assert p.type == "matype"
            elif p.type == "int":
                assert type(p.default) is int
            else:
                assert p.type == "float"
                assert type(p.default) is float
