"""Suite de contrato de motor parametrizable (B6.2; RF-10, RF-15, RF-21; RFC-0004 punto 5).

Uso desde los tests de cada motor (p. ej. ``tests/ft_vectorbt/test_contract.py``)::

    @pytest.mark.parametrize("kind", CONTRACT_KINDS)
    def test_contract(kind: str) -> None:
        assert_engine_matches_golden(VectorbtEngine(), kind)

La suite **obtiene las señales del núcleo** (registro de estrategias + ``finalize`` de
RF-16, igual que ``runner.run_backtest``) y solo llama a ``engine.run``: el motor nunca
recibe ``kind`` ni ``params``. La salida se valida con ``validate_engine_output`` y se
compara con el resultado canónico:

- las 3 integradas (``KINDS``) contra los *golden* versionados de ``golden/``;
- la estrategia sintética de RF-21 (``SYNTHETIC_KIND``, carpeta ``synthetic_strategy/``,
  registrada como local y no integrada) contra la referencia independiente
  (``reference.simulate``) ejecutada sobre las mismas señales del núcleo.

``assert_engine_modules_isolated`` es la comprobación AST de RF-15 (ningún módulo de motor
importa ``finances_core.backtest.strategies``/``catalog``), reutilizable por cada paquete
que implemente ``BacktestEngine``.
"""

from __future__ import annotations

import ast
import functools
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
from finances_core.backtest.catalog import BUILTIN_DIR, build_registry
from finances_core.backtest.engine_api import BacktestEngine, EngineOutput
from finances_core.backtest.errors import OutputOutOfRangeError
from finances_core.backtest.frames import validate_engine_output
from finances_core.backtest.metrics import compute
from finances_core.backtest.strategies.base import Signals
from finances_core.backtest.strategies.signals import finalize
from finances_core.schemas.backtest import CostModel

from tests.contract.fixture import GOLDEN_COSTS, GOLDEN_PARAMS, KINDS, TIMEFRAME, fixture_candles
from tests.contract.golden_io import golden_signals, golden_table, load_json
from tests.contract.reference import equity_frame, simulate, summarize, trades_frame

#: Raíz de estrategias locales con la estrategia sintética (una subcarpeta por ``kind``).
SYNTHETIC_DIR = Path(__file__).resolve().parent / "synthetic_strategy"
SYNTHETIC_KIND = "contract_synthetic"
#: ``kind`` que la suite admite, en el orden en que conviene parametrizar.
CONTRACT_KINDS: tuple[str, ...] = (*KINDS, SYNTHETIC_KIND)

#: Paquetes que contienen motores (``src/finances_backtester/<motor>/``). Un motor que
#: viva en otro paquete llama a ``assert_engine_modules_isolated`` con su propia raíz.
ENGINE_SOURCE_ROOTS: tuple[Path, ...] = (
    Path(__file__).resolve().parents[2] / "src" / "finances_backtester",
)
#: Módulos del núcleo que un motor no puede importar (RF-15, RF-21).
FORBIDDEN_ENGINE_MODULES = (
    "finances_core.backtest.strategies",
    "finances_core.backtest.catalog",
)


# --------------------------------------------------------------------------- tolerancias


@dataclass(frozen=True)
class Tolerance:
    """``|motor - canónico| <= abs_tol + rel_tol·|canónico|``; ``exact`` sin tolerancia."""

    rel_tol: float = 0.0
    abs_tol: float = 0.0
    exact: bool = False


EXACT = Tolerance(exact=True)
#: Precios de ejecución: 1e-6 relativo (≈ 8 ulp de ``float32``), RFC-0004 punto 5.
PRICE = Tolerance(rel_tol=1e-6)
#: Error absoluto mínimo de magnitudes monetarias (``pnl``, ``fees``, ``equity``): 1e-3
#: unidades de caja ≈ 1 ulp de ``float32`` a la escala de la caja de los *golden*
#: (10 000), para ``pnl`` cercanos a 0 donde el error relativo no tiene sentido.
MONEY_ABS_TOL = 1e-3
MONEY = Tolerance(rel_tol=1e-4, abs_tol=MONEY_ABS_TOL)
#: ``return`` es adimensional: mismo mínimo absoluto escalado por la caja inicial.
RATIO = Tolerance(rel_tol=1e-4, abs_tol=MONEY_ABS_TOL / GOLDEN_COSTS.initial_cash)
#: ``size`` (cantidad) se deriva de la caja como ``pnl``: misma clase relativa, sin mínimo.
SIZE = Tolerance(rel_tol=1e-4)
#: ``drawdown``: 1e-5 absoluto (RFC-0004 punto 5).
DRAWDOWN = Tolerance(abs_tol=1e-5)
#: Métricas del resumen (RF-7, recalculadas con ``metrics.compute``): 1e-3 relativo.
METRIC = Tolerance(rel_tol=1e-3, abs_tol=1e-6)

TOLERANCES: Mapping[str, Mapping[str, Tolerance]] = {
    "trades": {
        "entry_time_ms": EXACT,
        "exit_time_ms": EXACT,
        "side": EXACT,
        "entry_price": PRICE,
        "exit_price": PRICE,
        "size": SIZE,
        "fees": MONEY,
        "pnl": MONEY,
        "return": RATIO,
        "exit_reason": EXACT,
    },
    "equity": {"open_time_ms": EXACT, "equity": MONEY, "drawdown": DRAWDOWN},
}
#: Campos del resumen que se comparan exactos (conteos y caja inicial).
EXACT_SUMMARY_FIELDS = frozenset({"n_trades", "n_bars", "initial_cash"})
#: Campos de ``summary.json`` que no calcula ``metrics.compute`` (los añade el almacén).
STORE_ONLY_SUMMARY_FIELDS = frozenset({"start_ms", "end_ms", "warnings"})


@dataclass(frozen=True)
class Expected:
    """Resultado canónico de un ``kind`` sobre el *fixture*."""

    trades: pl.DataFrame
    equity: pl.DataFrame
    summary: Mapping[str, Any]


# --------------------------------------------------------------------------- señales


@functools.cache
def _registry_for(local_dir: Path | None) -> Any:
    return build_registry(builtin_dir=BUILTIN_DIR, local_dir=local_dir)


def _params_for(kind: str) -> Mapping[str, Any] | None:
    if kind in KINDS:
        return GOLDEN_PARAMS[kind]
    if kind == SYNTHETIC_KIND:
        return None  # default_params de la estrategia sintética
    msg = f"engine_contract: kind sin resultado canónico: {kind!r} (admitidos: {CONTRACT_KINDS})"
    raise ValueError(msg)


def core_signals(kind: str) -> Signals:
    """Señales normalizadas del núcleo (registro + ``finalize``) para ``kind`` en el *fixture*.

    Es exactamente lo que el ``runner`` entrega a ``engine.run``.

    Raises:
        ValueError: SI ``kind`` no está en ``CONTRACT_KINDS``.
    """
    raw_params = _params_for(kind)
    registry = _registry_for(None if kind in KINDS else SYNTHETIC_DIR)
    if kind not in registry.valid:
        msg = f"engine_contract: {kind!r} no registrada: {dict(registry.invalid)}"
        raise AssertionError(msg)
    strategy = registry.get(kind)
    params = strategy.Params.model_validate(
        strategy.default_params if raw_params is None else raw_params
    )
    candles = fixture_candles().to_frame()
    raw = strategy.signals(candles, params)
    signals: Signals = finalize(raw, n=candles.height, warmup=strategy.warmup(params))
    return signals


def _check_core_signals(kind: str, signals: Signals) -> None:
    """Precondición: las señales del núcleo igualan a los *golden* (B6.3)."""
    if kind not in KINDS:
        return
    entries, exits = golden_signals(kind)
    if signals.entries.to_list() != entries or signals.exits.to_list() != exits:
        msg = (
            f"engine_contract[{kind}]: las señales del núcleo difieren de signals_{kind}.json; "
            "es un defecto del núcleo (ver test_signals_golden.py), no del motor"
        )
        raise AssertionError(msg)


@functools.cache
def expected_output(kind: str) -> Expected:
    """Resultado canónico: *golden* para las integradas; referencia para la sintética."""
    if kind in KINDS:
        return Expected(
            trades=golden_table("trades", kind),
            equity=golden_table("equity", kind),
            summary=load_json(f"summary_{kind}.json"),
        )
    signals = core_signals(kind)
    candles = fixture_candles()
    sim = simulate(candles, signals.entries.to_list(), signals.exits.to_list(), GOLDEN_COSTS)
    summary = summarize(sim, candles, initial_cash=GOLDEN_COSTS.initial_cash, timeframe=TIMEFRAME)
    return Expected(trades=trades_frame(sim), equity=equity_frame(candles, sim), summary=summary)


# --------------------------------------------------------------------------- comparación


def _close(got: float, expected: float, tol: Tolerance) -> bool:
    if not (math.isfinite(got) and math.isfinite(expected)):
        return got == expected
    return abs(got - expected) <= tol.abs_tol + tol.rel_tol * abs(expected)


def _describe(tol: Tolerance) -> str:
    if tol.exact:
        return "exacto"
    return f"rel {tol.rel_tol:g}, abs {tol.abs_tol:g}"


def assert_tables_match(
    table: str,
    got: pl.DataFrame,
    expected: pl.DataFrame,
    tolerances: Mapping[str, Tolerance] | None = None,
    *,
    label: str = "",
) -> None:
    """Compara una tabla canónica (RF-6) columna a columna con las tolerancias de RFC-0004.

    Raises:
        AssertionError: con ``<tabla>.<columna> fila <i>: motor=… canónico=…`` en la
            primera discrepancia; el número de filas (``n_trades``) se compara antes.
    """
    rules = TOLERANCES[table] if tolerances is None else tolerances
    prefix = f"engine_contract{label}: "
    if got.height != expected.height:
        what = "n_trades" if table == "trades" else "n_bars"
        msg = f"{prefix}{table}: {what} motor={got.height} canónico={expected.height}"
        raise AssertionError(msg)
    if got.columns != expected.columns:
        msg = f"{prefix}{table}: columnas motor={got.columns} canónico={expected.columns}"
        raise AssertionError(msg)
    for column, tol in rules.items():
        values, reference = got[column].to_list(), expected[column].to_list()
        for row, (value, ref) in enumerate(zip(values, reference, strict=True)):
            same = value == ref if tol.exact else _close(float(value), float(ref), tol)
            if not same:
                msg = (
                    f"{prefix}{table}.{column} fila {row}: motor={value!r} canónico={ref!r} "
                    f"({_describe(tol)})"
                )
                raise AssertionError(msg)


def assert_summary_matches(
    got: Mapping[str, Any], expected: Mapping[str, Any], *, label: str = ""
) -> None:
    """Compara el resumen RF-7 recalculado con ``metrics.compute`` (1e-3 relativo)."""
    for name in sorted(expected.keys() - STORE_ONLY_SUMMARY_FIELDS):
        value, ref = got[name], expected[name]
        if name in EXACT_SUMMARY_FIELDS or value is None or ref is None:
            same = value == ref
        else:
            same = _close(float(value), float(ref), METRIC)
        if not same:
            msg = f"engine_contract{label}: summary.{name}: motor={value!r} canónico={ref!r}"
            raise AssertionError(msg)


def assert_output_matches(output: EngineOutput, expected: Expected, *, label: str = "") -> None:
    """Valida (RF-6) y compara ``trades``, ``equity`` y el resumen con ``expected``."""
    try:
        validate_engine_output(output, expected.equity.height)
    except OutputOutOfRangeError as exc:
        msg = f"engine_contract{label}: salida no canónica: {exc}"
        raise AssertionError(msg) from exc
    assert_tables_match("trades", output.trades, expected.trades, label=label)
    assert_tables_match("equity", output.equity, expected.equity, label=label)
    summary = compute(
        output.trades,
        output.equity,
        initial_cash=GOLDEN_COSTS.initial_cash,
        timeframe=TIMEFRAME,
    ).model_dump()
    assert_summary_matches(summary, expected.summary, label=label)


def assert_engine_matches_golden(engine: BacktestEngine, kind: str) -> None:
    """Ejecuta ``engine`` sobre el *fixture* con las señales del núcleo y lo compara.

    Exactos: ``n_trades``, ``entry_time_ms``, ``exit_time_ms``, ``side``, ``exit_reason`` y
    ``open_time_ms``; magnitudes con las tolerancias de RFC-0004 (``TOLERANCES``).

    Raises:
        ValueError: SI ``kind`` no está en ``CONTRACT_KINDS``.
        AssertionError: SI la salida no es canónica o difiere del resultado canónico.
    """
    _params_for(kind)
    signals = core_signals(kind)
    _check_core_signals(kind, signals)
    output = engine.run(
        fixture_candles().to_frame(),
        signals,
        CostModel(fees_bps=GOLDEN_COSTS.fees_bps, slippage_bps=GOLDEN_COSTS.slippage_bps),
        GOLDEN_COSTS.size_fraction,
        GOLDEN_COSTS.initial_cash,
    )
    assert_output_matches(output, expected_output(kind), label=f"[{engine.name}/{kind}]")


# --------------------------------------------------------------------------- RF-15 (AST)


def _forbidden(module: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in FORBIDDEN_ENGINE_MODULES)


def _imported_modules(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        yield node.module
        yield from (f"{node.module}.{alias.name}" for alias in node.names)
    elif (
        isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"import_module", "__import__"}:
            yield node.args[0].value


def engine_import_violations(source: str, relative: str) -> list[str]:
    """``<archivo>:<línea>: <módulo>`` por cada import prohibido a un motor (RF-15)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=relative)):
        for module in _imported_modules(node):
            if _forbidden(module):
                found.append(f"{relative}:{getattr(node, 'lineno', 0)}: {module}")
                break
    return found


def assert_engine_modules_isolated(*roots: Path) -> int:
    """Recorre los ``*.py`` de ``roots`` y falla si alguno importa estrategias o catálogo.

    Returns:
        Número de módulos inspeccionados (para que un test detecte un recorrido vacío).
    """
    scanned = 0
    violations: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root.parent).as_posix()
            violations.extend(engine_import_violations(path.read_text(encoding="utf-8"), relative))
            scanned += 1
    if violations:
        msg = "RF-15: un motor solo conoce Signals vía engine_api:\n" + "\n".join(violations)
        raise AssertionError(msg)
    return scanned
