"""Verificación de autor de estrategias (RF-20, plan §3.8).

:func:`assert_strategy_contract` comprueba, en este orden:

1. Estructura y README (RF-17), y que la carpeta se registró como válida.
2. Guarda AST (RF-11, RF-18).
3. Contrato de señales con ``default_params`` y cada ``check_params``: ``Boolean``,
   longitud correcta, sin nulos tras ``warmup``, sin excepciones, determinismo (dos
   llamadas idénticas) y sin mutar ``candles``.
4. Sin anticipación: para los cortes ``k`` de :func:`lookahead_cuts`,
   ``finalize(signals(candles[:k]))`` es idéntico a ``finalize(signals(candles))[:k]``.

Es una herramienta de autor (``kinds --check``, tests de las integradas), no una guarda
de ejecución: ejecuta el código de la estrategia en el proceso (A25, ADR-B13).

La serie sintética usa un generador PCG64 (XSL-RR 128/64) implementado aquí en Python
puro con semilla fija, para no depender de NumPy (RF-11).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import polars as pl
from pydantic import BaseModel

from finances_core.backtest.catalog import StrategyRegistry, get_registry
from finances_core.backtest.errors import InvalidSignalsError, StrategyInitError
from finances_core.backtest.strategies.checks import StrategyCheckError, check_ast, check_structure
from finances_core.backtest.strategies.discovery import StrategyClass
from finances_core.backtest.strategies.signals import finalize
from finances_core.config import FinancesSettings

SYNTHETIC_BARS = 600
SYNTHETIC_SEED = 20_261_102
_START_MS = 1_700_000_000_000
_HOUR_MS = 3_600_000

_MASK64 = (1 << 64) - 1
_MASK128 = (1 << 128) - 1
_PCG_MULTIPLIER = 0x2360ED051FC65DA44385DF649FCCF645
_PCG_STREAM = 0xDA3E39CB94B95BDB


class StrategyContractError(AssertionError):
    """La estrategia incumple la comprobación ``check`` (1..4) de RF-20."""

    def __init__(self, kind: str, check: int, message: str) -> None:
        super().__init__(f"{kind}: comprobación {check}: {message}")
        self.kind = kind
        self.check = check


class _Pcg64:
    """PCG64 (LCG de 128 bits + salida XSL-RR de 64 bits), determinista y sin NumPy."""

    def __init__(self, seed: int) -> None:
        self._inc = ((_PCG_STREAM << 1) | 1) & _MASK128
        self._state = 0
        self._step()
        self._state = (self._state + seed) & _MASK128
        self._step()

    def _step(self) -> None:
        self._state = (self._state * _PCG_MULTIPLIER + self._inc) & _MASK128

    def next_u64(self) -> int:
        self._step()
        state = self._state
        rotation = state >> 122
        mixed = ((state >> 64) ^ state) & _MASK64
        return ((mixed >> rotation) | (mixed << (-rotation & 63))) & _MASK64

    def uniform(self) -> float:
        """Uniforme en ``[0, 1)`` con 53 bits de mantisa."""
        return (self.next_u64() >> 11) / (1 << 53)


def synthetic_candles(n: int = SYNTHETIC_BARS, seed: int = SYNTHETIC_SEED) -> pl.DataFrame:
    """Velas RF-4 deterministas (``Int64``/``Float32``) con tendencia y rango.

    Cuatro tramos: alcista, rango, bajista y rango (reversión a un nivel).
    """
    rng = _Pcg64(seed)
    quarter = max(n // 4, 1)
    close = 100.0
    level = close
    rows: dict[str, list[Any]] = {name: [] for name in ("open", "high", "low", "close", "volume")}
    for t in range(n):
        segment = min(t // quarter, 3)
        if segment in (1, 3):
            drift = -0.05 * (close / level - 1.0)
        else:
            drift = 0.003 if segment == 0 else -0.003
            level = close
        previous = close
        close = previous * (1.0 + drift + (rng.uniform() - 0.5) * 0.02)
        rows["open"].append(previous)
        rows["close"].append(close)
        rows["high"].append(max(previous, close) * (1.0 + rng.uniform() * 0.005))
        rows["low"].append(min(previous, close) * (1.0 - rng.uniform() * 0.005))
        # Volumen determinista y estrictamente positivo (RF-4, L0-G1): sin ceros, para
        # que vwap/obv (que dividen por volumen acumulado) no degeneren en el sintético.
        rows["volume"].append(1000.0 + rng.uniform() * 500.0)
    times = [_START_MS + t * _HOUR_MS for t in range(n)]
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series(times, dtype=pl.Int64),
            **{name: pl.Series(values, dtype=pl.Float32) for name, values in rows.items()},
        }
    )


def lookahead_cuts(warmup: int, n: int) -> list[int]:
    """Cortes ``{warmup, warmup+1, n/4, n/2, 3n/4, n-1}`` válidos (``1 <= k <= n``), ordenados."""
    candidates = {warmup, warmup + 1, n // 4, n // 2, 3 * n // 4, n - 1}
    return sorted(k for k in candidates if 1 <= k <= n)


def _describe(params: BaseModel) -> str:
    return ", ".join(f"{key}={value}" for key, value in params.model_dump().items()) or "(vacío)"


def _run(
    kind: str, check: int, strategy: StrategyClass, candles: pl.DataFrame, params: BaseModel
) -> pl.Series:
    where = f"con {_describe(params)} y {candles.height} barras"
    warmup = strategy.warmup(params)
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise StrategyContractError(kind, 3, f"warmup debe ser un int >= 0 ({where})")
    try:
        raw = strategy.signals(candles, params)
    except (Exception, SystemExit) as exc:  # strategy_runtime_error: el autor puede lanzar todo
        message = f"signals lanzó {type(exc).__name__}: {exc} ({where})"
        raise StrategyContractError(kind, check, message) from None
    try:
        return finalize(raw, n=candles.height, warmup=warmup)
    except InvalidSignalsError as exc:
        raise StrategyContractError(kind, check, f"{exc} ({where})") from None


def _check_signals(kind: str, strategy: StrategyClass, params: BaseModel) -> pl.Series:
    candles = synthetic_candles()
    snapshot = candles.clone()
    first = _run(kind, 3, strategy, candles, params)
    if candles.schema != snapshot.schema or not candles.equals(snapshot):
        message = f"signals muta candles (con {_describe(params)})"
        raise StrategyContractError(kind, 3, message)
    second = _run(kind, 3, strategy, candles, params)
    if not first.equals(second):
        message = f"signals no es determinista: dos llamadas difieren (con {_describe(params)})"
        raise StrategyContractError(kind, 3, message)
    return first


def _first_difference(left: pl.Series, right: pl.Series) -> int:
    differs = (left != right).arg_true()
    return int(differs[0]) if differs.len() else -1


def _check_lookahead(
    kind: str, strategy: StrategyClass, params: BaseModel, full: pl.Series
) -> None:
    candles = synthetic_candles()
    for k in lookahead_cuts(strategy.warmup(params), candles.height):
        part = _run(kind, 4, strategy, candles.head(k), params)
        expected = full.head(k)
        got: pl.Series = part
        if not got.equals(expected):
            t = _first_difference(got, expected)
            message = (
                f"anticipación (look-ahead): k={k}: la barra t={t} de la posición cambia al "
                f"truncar las barras posteriores (con {_describe(params)})"
            )
            raise StrategyContractError(kind, 4, message)


def _params_to_check(strategy: StrategyClass) -> list[BaseModel]:
    sets: list[Mapping[str, Any]] = [strategy.default_params, *strategy.check_params]
    return [strategy.Params.model_validate(dict(values)) for values in sets]


def assert_strategy_contract(kind: str, registry: StrategyRegistry | None = None) -> None:
    """Ejecuta las comprobaciones 1–4 de RF-20 sobre la estrategia ``kind``.

    Args:
        kind: estrategia registrada (integrada o local).
        registry: por defecto, el del proceso (:func:`get_registry`).

    Raises:
        StrategyContractError: SI falla alguna comprobación (``check`` indica cuál).
        UnknownKindError: SI ``kind`` no está registrado.
    """
    active = registry if registry is not None else get_registry(FinancesSettings())
    try:
        strategy = active.get(kind)
    except StrategyInitError as exc:
        raise StrategyContractError(kind, 1, exc.reason) from None
    folder = active.folders[kind]
    for check, verify in ((1, check_structure), (2, check_ast)):
        try:
            verify(folder)
        except StrategyCheckError as exc:
            raise StrategyContractError(kind, check, str(exc)) from None
    for params in _params_to_check(strategy):
        full = _check_signals(kind, strategy, params)
        _check_lookahead(kind, strategy, params, full)
