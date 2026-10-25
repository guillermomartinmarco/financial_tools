"""Catálogo cerrado de ``ft_indicators`` (RF-2, RF-4, RF-6; plan §3.1).

``CATALOG`` define, por indicador: parámetros (orden = orden en ``indicator_id``),
entradas, salidas, calentamiento, grafo de convergencia (RF-16), tolerancias ``f32``
(§3.9), equivalente TA-Lib con sus ajustes de test (§3.11) y kernel ``Float64`` (§3.3).
:func:`resolve` valida una :class:`IndicatorSpec` y completa sus defaults.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Literal

from finances_core.indicators_store.namespaces import validate_series_id
from finances_core.schemas.indicators import (
    IndicatorCatalogEntry,
    IndicatorName,
    IndicatorSpec,
    Namespace,
    TalibEquivalentInfo,
    series_id,
)

from .convergence import EPSILON, StabilityKind, Stage, k_star, stable_index
from .errors import IndicatorValidationError
from .kernels import cumulative, recursive, windowed
from .kernels.common import Outputs, Params, ParamValue
from .precision import TOLERANCES, output_key

#: Versión de fórmulas del motor (``StoreEntry.engine_version = "ft_indicators:{n}"``).
ENGINE_VERSION = 1
NAMESPACE: Namespace = "ft_indicators"
PRICE_SOURCES = ("open", "high", "low", "close", "adj_close")
DEFAULT_SOURCE = "close"
MAX_PERIOD = 10_000

Kernel = Callable[..., Outputs]
CompareFrom = Literal["first_non_null", "stable_from"] | int


@dataclass(frozen=True)
class ParamDef:
    """Parámetro de un indicador con su rango (RF-2)."""

    name: str
    type: type[int] | type[float] | type[str]
    default: ParamValue
    min: float | None = None
    max: float | None = None
    min_exclusive: bool = False
    max_decimals: int | None = None
    choices: tuple[str, ...] | None = None

    def valid_text(self) -> str:
        """Rango legible (p. ej. ``[2, 10000]`` o ``(0, 10] con como máximo 2 decimales``)."""
        if self.choices is not None:
            return f"uno de {list(self.choices)}"
        low = "(" if self.min_exclusive else "["
        lo = f"{self.min:g}" if self.min is not None else "-inf"
        hi = f"{self.max:g}" if self.max is not None else "inf"
        text = f"{low}{lo}, {hi}]"
        if self.max_decimals is not None:
            text += f" con como máximo {self.max_decimals} decimales"
        return text

    def as_public(self) -> dict[str, object]:
        """Forma publicada en ``GET /catalog``."""
        out: dict[str, object] = {"type": self.type.__name__, "default": self.default}
        if self.choices is not None:
            out["choices"] = list(self.choices)
        else:
            out["min"] = int(self.min) if self.type is int and self.min is not None else self.min
            out["max"] = int(self.max) if self.type is int and self.max is not None else self.max
        if self.min_exclusive:
            out["min_exclusive"] = True
        if self.max_decimals is not None:
            out["max_decimals"] = self.max_decimals
        return out


@dataclass(frozen=True)
class TalibEquivalent:
    """Equivalente TA-Lib y ajustes explícitos del test de paridad (spec RF-2, plan §3.11).

    ``params``: parámetro TA-Lib → nombre de parámetro propio o literal numérico.
    ``outputs``: sufijo propio (``""`` = columna base) → ``"FUNCION:salida"`` de TA-Lib.
    """

    function: str
    params: Mapping[str, str]
    outputs: Mapping[str, str]
    compare_from: Callable[[Params], CompareFrom] = lambda _p: "first_non_null"
    excluded_rows: str | None = None


def _stages_none(_params: Params) -> Mapping[str, Stage]:
    return {}


@dataclass(frozen=True)
class IndicatorDef:
    """Definición de un indicador del catálogo (plan §3.1)."""

    name: IndicatorName
    params: tuple[ParamDef, ...]
    fixed_inputs: tuple[str, ...]  # () = una serie elegida con ``source``
    outputs: tuple[str, ...]  # sufijos; "" = columna base ``{indicator_id}``
    warmup: Callable[[Params], Mapping[str, int]]  # por sufijo
    warmup_text: str
    kind: StabilityKind
    stable_from_text: str
    talib: TalibEquivalent | None
    kernel: Kernel
    convention: str
    stages: Callable[[Params], Mapping[str, Stage]] = field(default=_stages_none)
    cross_check: Callable[[Params], None] | None = None
    engine_version: int = ENGINE_VERSION

    def param(self, name: str) -> ParamDef | None:
        """Definición del parámetro ``name`` (``None`` si no existe)."""
        return next((p for p in self.params if p.name == name), None)


@dataclass(frozen=True)
class ResolvedIndicator:
    """Indicador validado con todos sus parámetros, ``indicator_id`` y columnas (RF-4)."""

    definition: IndicatorDef
    params: dict[str, ParamValue]
    indicator_id: str
    columns: dict[str, str]  # sufijo → columna
    inputs: tuple[str, ...]

    @property
    def kind(self) -> StabilityKind:
        return self.definition.kind

    @property
    def stages(self) -> dict[str, Stage]:
        """Etapas por **columna** (vacío en no recursivos)."""
        return {
            self.columns[suffix]: stage
            for suffix, stage in self.definition.stages(self.params).items()
        }

    def spec(self) -> IndicatorSpec:
        """``IndicatorSpec`` con defaults resueltos."""
        return IndicatorSpec(name=self.definition.name, params=dict(self.params))

    def warmups(self) -> dict[str, int]:
        """Calentamiento (filas nulas iniciales sobre serie sin nulos) por columna."""
        return {
            self.columns[suffix]: rows
            for suffix, rows in self.definition.warmup(self.params).items()
        }


# ----------------------------------------------------------------------------- ayudas


def _period(default: int, minimum: int = 2) -> ParamDef:
    return ParamDef("period", int, default, min=minimum, max=MAX_PERIOD)


def _int(name: str, default: int) -> ParamDef:
    return ParamDef(name, int, default, min=2, max=MAX_PERIOD)


SOURCE = ParamDef("source", str, DEFAULT_SOURCE, choices=PRICE_SOURCES)


def _p(params: Params, name: str) -> int:
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} no resuelto como entero"
        raise TypeError(msg)
    return value


def _ewm(seed: int, period: int, *, wilder: bool, inputs: tuple[Stage, ...] = ()) -> Stage:
    if wilder:
        return Stage(seed, 1.0 / period, f"1/{period}", inputs)
    return Stage(seed, 2.0 / (period + 1), f"2/{period + 1}", inputs)


def _single(rows: Callable[[Params], int]) -> Callable[[Params], Mapping[str, int]]:
    return lambda p: {"": rows(p)}


def _default_index(stage: Stage) -> str:
    return f"{stable_index(stage)}"


# --------------------------------------------------------------------------- etapas


def _ema_stages(p: Params) -> Mapping[str, Stage]:
    n = _p(p, "period")
    return {"": _ewm(n - 1, n, wilder=False)}


def _wilder_stages(p: Params) -> Mapping[str, Stage]:
    n = _p(p, "period")
    return {"": _ewm(n, n, wilder=True)}


def _macd_stages(p: Params) -> Mapping[str, Stage]:
    fast, slow, signal = _p(p, "fast"), _p(p, "slow"), _p(p, "signal")
    line = Stage(inputs=(_ewm(fast - 1, fast, wilder=False), _ewm(slow - 1, slow, wilder=False)))
    sig = _ewm(slow + signal - 2, signal, wilder=False, inputs=(line,))
    return {"line": line, "signal": sig, "hist": Stage(inputs=(line, sig))}


def _adx_stages(p: Params) -> Mapping[str, Stage]:
    n = _p(p, "period")
    di = _ewm(n - 1, n, wilder=True)  # semilla TA-Lib: Σ de period−1 valores en t = period−1
    dx = Stage(inputs=(di,))
    return {"": _ewm(2 * n - 1, n, wilder=True, inputs=(dx,)), "plus_di": di, "minus_di": di}


def _macd_check(p: Params) -> None:
    if _p(p, "fast") >= _p(p, "slow"):
        msg = f"macd: se exige fast < slow (fast={p['fast']}, slow={p['slow']})"
        raise IndicatorValidationError(
            msg, indicator="macd", param="fast", value=p["fast"], valid="fast < slow"
        )


_EWM_RULE = "k* = ceil(ln {eps:g} / ln(1 - alpha)); estable = max(semilla, entrada) + k*"
_RULE = _EWM_RULE.format(eps=EPSILON)

CATALOG: Mapping[str, IndicatorDef] = {
    "sma": IndicatorDef(
        name="sma",
        params=(_period(20), SOURCE),
        fixed_inputs=(),
        outputs=("",),
        warmup=_single(lambda p: _p(p, "period") - 1),
        warmup_text="period-1",
        kind="windowed",
        stable_from_text="primera fila no nula (sin semilla)",
        talib=TalibEquivalent("SMA", {"timeperiod": "period"}, {"": "SMA:real"}),
        kernel=windowed.sma,
        convention="Media aritmética de las últimas period filas",
    ),
    "ema": IndicatorDef(
        name="ema",
        params=(_period(20), SOURCE),
        fixed_inputs=(),
        outputs=("",),
        warmup=_single(lambda p: _p(p, "period") - 1),
        warmup_text="period-1",
        kind="recursive",
        stable_from_text=(
            f"EMA alpha=2/(period+1) sembrada en period-1; {_RULE} "
            f"(default: k*={k_star(2 / 21)}, índice {_default_index(_ewm(19, 20, wilder=False))})"
        ),
        talib=TalibEquivalent("EMA", {"timeperiod": "period"}, {"": "EMA:real"}),
        kernel=recursive.ema,
        stages=_ema_stages,
        convention=(
            "alpha = 2/(period+1); semilla = SMA de las primeras period filas; "
            "y_t = y_{t-1} + alpha*(x_t - y_{t-1}) (TA-Lib classic / StockCharts)"
        ),
    ),
    "wma": IndicatorDef(
        name="wma",
        params=(_period(20), SOURCE),
        fixed_inputs=(),
        outputs=("",),
        warmup=_single(lambda p: _p(p, "period") - 1),
        warmup_text="period-1",
        kind="windowed",
        stable_from_text="primera fila no nula (sin semilla)",
        talib=TalibEquivalent("WMA", {"timeperiod": "period"}, {"": "WMA:real"}),
        kernel=windowed.wma,
        convention="Pesos lineales 1..period (el más reciente pesa period) / suma de pesos",
    ),
    "rsi": IndicatorDef(
        name="rsi",
        params=(_period(14), SOURCE),
        fixed_inputs=(),
        outputs=("",),
        warmup=_single(lambda p: _p(p, "period")),
        warmup_text="period",
        kind="recursive",
        stable_from_text=(
            f"Wilder alpha=1/period sobre G y L sembrado en period; {_RULE} "
            f"(default: k*={k_star(1 / 14)}, índice {_default_index(_ewm(14, 14, wilder=True))})"
        ),
        talib=TalibEquivalent("RSI", {"timeperiod": "period"}, {"": "RSI:real"}),
        kernel=recursive.rsi,
        stages=_wilder_stages,
        convention=(
            "Wilder (1978): G, L = media simple de las primeras period ganancias/pérdidas "
            "(semilla en t=period), luego Wilder alpha=1/period; RSI = 100*G/(G+L); "
            "G+L = 0 -> 0 (TA-Lib)"
        ),
    ),
    "macd": IndicatorDef(
        name="macd",
        params=(_int("fast", 12), _int("slow", 26), _int("signal", 9), SOURCE),
        fixed_inputs=(),
        outputs=("line", "signal", "hist"),
        warmup=lambda p: {
            "line": _p(p, "slow") - 1,
            "signal": _p(p, "slow") + _p(p, "signal") - 2,
            "hist": _p(p, "slow") + _p(p, "signal") - 2,
        },
        warmup_text="line: slow-1; signal/hist: slow+signal-2",
        kind="recursive",
        stable_from_text=(
            "line = max(estable EMA fast, estable EMA slow); signal = max(slow+signal-2, line) "
            f"+ k*(2/(signal+1)); hist = max(line, signal); {_RULE} "
            "(default: line 205, signal/hist 267)"
        ),
        talib=TalibEquivalent(
            "MACD",
            {"fastperiod": "fast", "slowperiod": "slow", "signalperiod": "signal"},
            {"line": "MACD:macd", "signal": "MACD:macdsignal", "hist": "MACD:macdhist"},
            compare_from=lambda _p: "stable_from",
        ),
        kernel=recursive.macd,
        stages=_macd_stages,
        cross_check=_macd_check,
        convention=(
            "line = EMA(fast) - EMA(slow), cada EMA con su propia semilla SMA (Appel / "
            "StockCharts; TA-Lib alinea las semillas); signal = EMA(signal) de line con "
            "semilla SMA; hist = line - signal"
        ),
    ),
    "bb": IndicatorDef(
        name="bb",
        params=(
            _period(20),
            ParamDef("std", float, 2.0, min=0, max=10, min_exclusive=True, max_decimals=2),
            SOURCE,
        ),
        fixed_inputs=(),
        outputs=("middle", "upper", "lower"),
        warmup=lambda p: dict.fromkeys(("middle", "upper", "lower"), _p(p, "period") - 1),
        warmup_text="period-1",
        kind="windowed",
        stable_from_text="primera fila no nula (sin semilla)",
        talib=TalibEquivalent(
            "BBANDS",
            {"timeperiod": "period", "nbdevup": "std", "nbdevdn": "std", "matype": "0"},
            {
                "middle": "BBANDS:middleband",
                "upper": "BBANDS:upperband",
                "lower": "BBANDS:lowerband",
            },
        ),
        kernel=windowed.bb,
        convention="middle = SMA; sigma poblacional (ddof=0); upper/lower = middle ± std*sigma",
    ),
    "atr": IndicatorDef(
        name="atr",
        params=(_period(14),),
        fixed_inputs=("high", "low", "close"),
        outputs=("",),
        warmup=_single(lambda p: _p(p, "period")),
        warmup_text="period",
        kind="recursive",
        stable_from_text=(
            f"Wilder alpha=1/period sembrado en period; {_RULE} "
            f"(default: índice {_default_index(_ewm(14, 14, wilder=True))})"
        ),
        talib=TalibEquivalent("ATR", {"timeperiod": "period"}, {"": "ATR:real"}),
        kernel=recursive.atr,
        stages=_wilder_stages,
        convention=(
            "TR_t = max(H-L, |H-C_{t-1}|, |L-C_{t-1}|) desde t=1; semilla = media de "
            "TR[1..period] en t=period; luego Wilder (como TA-Lib)"
        ),
    ),
    "stoch": IndicatorDef(
        name="stoch",
        params=(_int("k", 14), _int("smooth_k", 3), _int("d", 3)),
        fixed_inputs=("high", "low", "close"),
        outputs=("k", "d"),
        warmup=lambda p: {
            "k": _p(p, "k") + _p(p, "smooth_k") - 2,
            "d": _p(p, "k") + _p(p, "smooth_k") + _p(p, "d") - 3,
        },
        warmup_text="k: k+smooth_k-2; d: k+smooth_k+d-3",
        kind="windowed",
        stable_from_text="primera fila no nula (sin semilla)",
        talib=TalibEquivalent(
            "STOCH",
            {
                "fastk_period": "k",
                "slowk_period": "smooth_k",
                "slowk_matype": "0",
                "slowd_period": "d",
                "slowd_matype": "0",
            },
            {"k": "STOCH:slowk", "d": "STOCH:slowd"},
            compare_from=lambda p: _p(p, "k") + _p(p, "smooth_k") + _p(p, "d") - 3,
        ),
        kernel=windowed.stoch,
        convention=(
            "%K rápido = 100*(C - LL_k)/(HH_k - LL_k) (HH = LL -> 0, TA-Lib); "
            "%K = SMA(%K rápido, smooth_k); %D = SMA(%K, d) (Full Stochastic de StockCharts)"
        ),
    ),
    "roc": IndicatorDef(
        name="roc",
        params=(_period(12, minimum=1), SOURCE),
        fixed_inputs=(),
        outputs=("",),
        warmup=_single(lambda p: _p(p, "period")),
        warmup_text="period",
        kind="windowed",
        stable_from_text="primera fila no nula (sin semilla)",
        talib=TalibEquivalent(
            "ROC",
            {"timeperiod": "period"},
            {"": "ROC:real"},
            excluded_rows="base x_{t-period} == 0 (TA-Lib devuelve 0; ft_indicators nulo)",
        ),
        kernel=windowed.roc,
        convention="100*(x_t / x_{t-period} - 1); base 0 -> nulo",
    ),
    "obv": IndicatorDef(
        name="obv",
        params=(),
        fixed_inputs=("close", "volume"),
        outputs=("",),
        warmup=_single(lambda _p: 0),
        warmup_text="0",
        kind="non_decaying",
        stable_from_text="nulo: non_decaying_seed (el nivel depende para siempre de volume_0)",
        talib=TalibEquivalent("OBV", {}, {"": "OBV:real"}),
        kernel=cumulative.obv,
        convention=(
            "OBV_0 = volume_0 (TA-Lib); OBV_t = OBV_{t-1} ± volume_t según C_t >/< C_{t-1}; "
            "igual si C_t = C_{t-1}"
        ),
    ),
    "adx": IndicatorDef(
        name="adx",
        params=(_period(14),),
        fixed_inputs=("high", "low", "close"),
        outputs=("", "plus_di", "minus_di"),
        warmup=lambda p: {
            "": 2 * _p(p, "period") - 1,
            "plus_di": _p(p, "period"),
            "minus_di": _p(p, "period"),
        },
        warmup_text="+DI/-DI: period; ADX: 2*period-1",
        kind="recursive",
        stable_from_text=(
            "±DI: Wilder alpha=1/period sembrado en period-1 (semilla TA-Lib); "
            f"ADX: max(2*period-1, ±DI) + k*; {_RULE} (default: ±DI 200, ADX 387)"
        ),
        talib=TalibEquivalent(
            "ADX",
            {"timeperiod": "period"},
            {"": "ADX:real", "plus_di": "PLUS_DI:real", "minus_di": "MINUS_DI:real"},
        ),
        kernel=recursive.adx,
        stages=_adx_stages,
        convention=(
            "Wilder con la semilla de TA-Lib: +DM/-DM y TR desde t=1; S = suma de los primeros "
            "period-1 valores y un paso de Wilder en t=period (S_p = S*(1-1/p) + x_p), "
            "luego S_t = S_{t-1} - S_{t-1}/period + x_t; ±DI = 100*S_DM/S_TR (S_TR = 0 -> 0); "
            "DX = 100*|+DI - -DI|/(+DI + -DI) (suma 0 -> 0); ADX semilla = media de los "
            "primeros period DX, luego Wilder"
        ),
    ),
    "vwap": IndicatorDef(
        name="vwap",
        params=(_period(20),),
        fixed_inputs=("high", "low", "close", "volume"),
        outputs=("",),
        warmup=_single(lambda p: _p(p, "period") - 1),
        warmup_text="period-1",
        kind="windowed",
        stable_from_text="primera fila no nula (sin semilla)",
        talib=None,
        kernel=windowed.vwap,
        convention=(
            "VWAP móvil: sum(TP*V)/sum(V) en period filas, TP = (H+L+C)/3; sum(V) = 0 -> nulo"
        ),
    ),
}

# ------------------------------------------------------------------------- resolución


def _invalid(
    defn: IndicatorDef, param: ParamDef, value: object, reason: str
) -> IndicatorValidationError:
    msg = (
        f"{defn.name}: parámetro {param.name}={value!r} inválido ({reason}); "
        f"válido: {param.valid_text()}"
    )
    return IndicatorValidationError(
        msg, indicator=defn.name, param=param.name, value=value, valid=param.valid_text()
    )


def _coerce(defn: IndicatorDef, param: ParamDef, value: object) -> ParamValue:
    if isinstance(value, bool):
        raise _invalid(defn, param, value, "booleano no admitido")
    if param.type is str:
        if (
            not isinstance(value, str)
            or param.choices is None
            or value.lower() not in param.choices
        ):
            raise _invalid(defn, param, value, "valor no admitido")
        return value.lower()
    if param.type is int:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise _invalid(defn, param, value, "se espera un entero")
        number: int | float = value
    else:
        if not isinstance(value, int | float):
            raise _invalid(defn, param, value, "se espera un valor numérico")
        number = float(value)
        if param.max_decimals is not None and round(number, param.max_decimals) != number:
            raise _invalid(defn, param, value, "demasiados decimales")
    too_low = param.min is not None and (
        number <= param.min if param.min_exclusive else number < param.min
    )
    too_high = param.max is not None and number > param.max
    if too_low or too_high:
        raise _invalid(defn, param, value, "fuera de rango")
    return number


def resolve(
    spec: IndicatorSpec, available_columns: Collection[str] | None = None
) -> ResolvedIndicator:
    """Valida ``spec`` contra el catálogo y completa defaults (RF-2, RF-4, RF-6).

    Args:
        spec: petición de un indicador.
        available_columns: columnas del dataset; ``None`` omite la comprobación de
            existencia de la columna ``source`` (validación sin dataset).

    Raises:
        IndicatorValidationError: SI el indicador no existe, un parámetro no existe para
            ese indicador (incluido ``source`` en los indicadores OHLC), un valor está fuera
            de rango, ``macd.fast >= macd.slow``, o ``source`` no existe en el dataset.
    """
    name = spec.name.lower()
    defn = CATALOG.get(name)
    if defn is None:
        msg = f"Indicador desconocido {spec.name!r}; válidos: {list(CATALOG)}"
        raise IndicatorValidationError(msg, indicator=spec.name, valid=", ".join(CATALOG))
    known = {p.name for p in defn.params}
    for key in spec.params:
        if key not in known:
            valid = ", ".join(sorted(known)) or "ninguno"
            msg = f"{name}: parámetro inexistente {key!r}; parámetros válidos: {valid}"
            raise IndicatorValidationError(
                msg, indicator=name, param=key, value=spec.params[key], valid=valid
            )
    params: dict[str, ParamValue] = {}
    for param in defn.params:
        raw = spec.params.get(param.name, param.default)
        params[param.name] = _coerce(defn, param, raw)
    if defn.cross_check is not None:
        defn.cross_check(params)

    source = params.get("source")
    inputs = defn.fixed_inputs or (str(source),)
    if available_columns is not None:
        missing = [c for c in inputs if c not in available_columns]
        if missing:
            msg = f"{name}: columnas de entrada ausentes en el dataset: {missing}"
            raise IndicatorValidationError(
                msg,
                indicator=name,
                param="source",
                value=source,
                valid=str(sorted(available_columns)),
            )
    tokens = [params[p.name] for p in defn.params if p.name != "source"]
    overrides = [str(source)] if source is not None and source != DEFAULT_SOURCE else []
    indicator_id = validate_series_id(NAMESPACE, series_id("", name, tokens, overrides))
    columns = {s: indicator_id if not s else f"{indicator_id}_{s}" for s in defn.outputs}
    return ResolvedIndicator(
        definition=defn, params=params, indicator_id=indicator_id, columns=columns, inputs=inputs
    )


# ------------------------------------------------------------------------ publicación


def _talib_info(defn: IndicatorDef, defaults: Params) -> TalibEquivalentInfo | None:
    if defn.talib is None:
        return None
    return TalibEquivalentInfo(
        function=defn.talib.function,
        params=dict(defn.talib.params),
        outputs={output_key(defn.name, s): v for s, v in defn.talib.outputs.items()},
        compare_from=defn.talib.compare_from(defaults),
        excluded_rows=defn.talib.excluded_rows,
    )


def catalog() -> list[IndicatorCatalogEntry]:
    """Entradas de ``GET /indicators/catalog`` (orden de RF-2)."""
    entries = []
    for defn in CATALOG.values():
        defaults = resolve(IndicatorSpec(name=defn.name)).params
        entries.append(
            IndicatorCatalogEntry(
                name=defn.name,
                params={p.name: p.as_public() for p in defn.params},
                inputs=list(defn.fixed_inputs or ("source",)),
                outputs=[] if defn.outputs == ("",) else list(defn.outputs),
                warmup=defn.warmup_text,
                stable_from=defn.stable_from_text,
                f32_tolerance={key: tol.as_dict() for key, tol in TOLERANCES[defn.name].items()},
                talib_equivalent=_talib_info(defn, defaults),
                engine_version=defn.engine_version,
                convention=defn.convention,
            )
        )
    return entries


__all__ = [
    "CATALOG",
    "ENGINE_VERSION",
    "NAMESPACE",
    "IndicatorDef",
    "ParamDef",
    "ResolvedIndicator",
    "TalibEquivalent",
    "catalog",
    "resolve",
]
