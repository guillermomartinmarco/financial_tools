"""Motor VectorBT — ÚNICO módulo con pandas/NumPy de doble precisión y ``vectorbt`` (RFC-0004).

Exactamente dos puntos de conversión (``specs/ft_vectorbt/plan.md`` §3.1):

1. :func:`to_framework_input`: velas canónicas ``Float32`` y ``Signals`` ``Boolean`` →
   arreglos NumPy de doble precisión (ampliación exacta) y booleanos (sin pérdida), con
   índice **posicional**: ``open_time_ms`` nunca entra al framework.
2. :func:`from_framework_output`: arreglos del framework → ``trades``/``equity`` canónicos
   (``Float32``/``Int64``/``Int8``, RF-6), con los tiempos recuperados por posición de la
   ``Series`` ``Int64`` original y rechazo (``output_out_of_range``) de valores no finitos,
   fuera de dominio o de índices fuera del rango; nunca trunca ni corrige.

:func:`simulate` opera solo sobre lo creado en el punto 1 y traduce las señales al modelo
de ejecución de ``finances_backtester`` RF-5 con ``vectorbt.Portfolio.from_signals``
(plan §3.3). El motor no conoce la estrategia: no calcula señales ni indicadores (RF-1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import polars as pl
import vectorbt as vbt  # type: ignore[import-untyped, unused-ignore]  # sin py.typed ni stubs; unused-ignore tolera entornos donde vectorbt resuelve con tipos (CI py3.12)
from finances_core.backtest.costs import fee_rate, slip
from finances_core.backtest.engine_api import CostModel, EngineOutput, RiskModel, Signals
from finances_core.backtest.errors import OutputOutOfRangeError
from finances_core.backtest.frames import (
    EQUITY_SCHEMA,
    EXIT_REASON_DOMAIN,
    EXIT_REASON_END_OF_DATA,
    EXIT_REASON_SIGNAL,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    SIDE_DOMAIN,
    SIDE_LONG,
    SIDE_SHORT,
    TRADES_SCHEMA,
    validate_engine_output,
)

from finances_backtester.ft_vectorbt import ENGINE_NAME

__all__ = [
    "FrameworkInput",
    "FrameworkResult",
    "VectorbtEngine",
    "from_framework_output",
    "simulate",
    "to_framework_input",
]

FloatArray = npt.NDArray[np.floating[Any]]
IntArray = npt.NDArray[np.integer[Any]]
BoolArray = npt.NDArray[np.bool_]

#: ``direction`` de los registros de operaciones de VectorBT (``TradeDirection``).
_VBT_LONG = 0
#: ``status`` de una operación abierta en VectorBT (``TradeStatus.Open``).
_VBT_OPEN = 0


@dataclass(frozen=True)
class FrameworkInput:
    """Entrada de VectorBT: precios en doble precisión y señales booleanas, por posición.

    ``short_entries``/``short_exits`` (Enmienda G2b) van siempre poblados (todo ``False``
    cuando la estrategia no opera en corto). ``high``/``low`` (Enmienda G4) alimentan la
    detección intrabar de stops de ``vbt.Portfolio.from_signals``.
    """

    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    entries: BoolArray
    exits: BoolArray
    short_entries: BoolArray
    short_exits: BoolArray

    @property
    def n_bars(self) -> int:
        """Número de barras (índice posicional ``0..n-1``)."""
        return int(self.open.shape[0])


@dataclass(frozen=True)
class FrameworkResult:
    """Salida de :func:`simulate` en doble precisión, indexada por posición de barra.

    Una entrada por operación cerrada (``entry_idx``…``exit_reason``, ``return_`` es la
    columna ``return`` de RF-6) y una por barra (``equity``, ``drawdown``).
    """

    entry_idx: IntArray
    exit_idx: IntArray
    side: IntArray
    entry_price: FloatArray
    exit_price: FloatArray
    size: FloatArray
    fees: FloatArray
    pnl: FloatArray
    return_: FloatArray
    exit_reason: IntArray
    equity: FloatArray
    drawdown: FloatArray


# --------------------------------------------------------------------------- punto 1


def to_framework_input(candles: pl.DataFrame, signals: Signals) -> FrameworkInput:
    """PUNTO DE CONVERSIÓN 1: ``Float32`` → doble precisión (exacta) y señales booleanas.

    Solo ``open`` y ``close`` entran al framework; ``open_time_ms`` se queda fuera (índice
    posicional, RFC-0004 punto 3).

    Raises:
        TypeError: SI ``open``/``close`` no son ``Float32`` (la ampliación dejaría de ser
            exacta respecto al dato canónico).
        ValueError: SI alguna de las 4 señales no tiene la longitud de las velas.
    """
    for column in ("open", "high", "low", "close"):
        if candles.schema[column] != pl.Float32:
            msg = f"to_framework_input: {column} es {candles.schema[column]}, se espera Float32"
            raise TypeError(msg)
    n = candles.height
    lengths = {
        "entries": signals.entries.len(),
        "exits": signals.exits.len(),
        "entries_short": signals.entries_short.len(),
        "exits_short": signals.exits_short.len(),
    }
    if any(length != n for length in lengths.values()):
        msg = f"to_framework_input: longitud de señales {lengths} != {n} barras"
        raise ValueError(msg)
    return FrameworkInput(
        open=candles["open"].to_numpy().astype(np.float64),
        high=candles["high"].to_numpy().astype(np.float64),
        low=candles["low"].to_numpy().astype(np.float64),
        close=candles["close"].to_numpy().astype(np.float64),
        entries=signals.entries.to_numpy().astype(np.bool_),
        exits=signals.exits.to_numpy().astype(np.bool_),
        short_entries=signals.entries_short.to_numpy().astype(np.bool_),
        short_exits=signals.exits_short.to_numpy().astype(np.bool_),
    )


# --------------------------------------------------------------------------- simulación


def _shift(flags: BoolArray) -> BoolArray:
    """Señal al cierre de ``t`` → orden en la barra ``t+1`` (RF-5.1)."""
    shifted = np.zeros_like(flags)
    shifted[1:] = flags[:-1]
    return shifted


def _drawdown(equity: FloatArray, initial_cash: float) -> FloatArray:
    """``equity / pico - 1`` con el pico iniciado en la caja inicial (como la referencia)."""
    peak = np.maximum.accumulate(np.maximum(equity, initial_cash))
    drawdown: FloatArray = equity / peak - 1.0
    return drawdown


def _classify_stop_reason(  # noqa: PLR0913 - parámetros de una operación cerrada (Enmienda G4)
    *,
    side: int,
    entry_idx: int,
    exit_idx: int,
    raw_entry: float,
    high: FloatArray,
    low: FloatArray,
    open_: FloatArray,
    risk: RiskModel,
    default_reason: int,
) -> int:
    """Clasifica el motivo de salida de una operación cerrada bajo stops (Enmienda G4).

    Re-deriva, barra a barra sobre ``[entry_idx+1, exit_idx]`` (la barra de entrada se
    excluye, como hace VectorBT), el primer stop que dispara contra las mechas de la vela;
    el precio y el índice de salida ya los fijó VectorBT, esto solo decide la etiqueta. El
    nivel se calcula contra ``raw_entry`` (``open`` de la barra de entrada, pre-slippage: la
    base que usa VectorBT). El stop trailing sigue el pico de ``high``/``low`` **previo** a
    la barra evaluada (la entrada no cuenta). En empate en la misma barra gana el stop-loss.
    Si ningún stop dispara hasta ``exit_idx``, la salida conserva ``default_reason`` (señal
    o fin de datos).
    """
    sign = 1.0 if side == SIDE_LONG else -1.0
    sl_frac = risk.sl_frac
    tp_frac = risk.tp_frac
    tp_level = raw_entry * (1.0 + sign * tp_frac) if tp_frac is not None else None
    extreme = raw_entry  # pico (largo) / valle (corto) del trailing, sin la barra de entrada
    for j in range(entry_idx + 1, exit_idx + 1):
        sl_level: float | None = None
        if sl_frac is not None:
            basis = extreme if risk.sl_trail else raw_entry
            sl_level = basis * (1.0 - sign * sl_frac)
        if side == SIDE_LONG:
            sl_hit = sl_level is not None and (low[j] <= sl_level or open_[j] <= sl_level)
            tp_hit = tp_level is not None and (high[j] >= tp_level or open_[j] >= tp_level)
        else:
            sl_hit = sl_level is not None and (high[j] >= sl_level or open_[j] >= sl_level)
            tp_hit = tp_level is not None and (low[j] <= tp_level or open_[j] <= tp_level)
        if sl_hit or tp_hit:
            return EXIT_REASON_STOP_LOSS if sl_hit else EXIT_REASON_TAKE_PROFIT
        if risk.sl_trail and sl_frac is not None:
            extreme = max(extreme, high[j]) if side == SIDE_LONG else min(extreme, low[j])
    return default_reason


def _stop_exit_reasons(  # noqa: PLR0913, PLR0917 - registros de operaciones + stops (Enmienda G4)
    inp: FrameworkInput,
    entry_idx: IntArray,
    exit_idx: IntArray,
    side: IntArray,
    base_reason: IntArray,
    risk: RiskModel,
) -> IntArray:
    """``exit_reason`` de cada operación reclasificando las salidas por stop (Enmienda G4).

    Parte de ``base_reason`` (señal / fin de datos) y reemplaza por ``stop_loss`` /
    ``take_profit`` las operaciones cuyo cierre disparó un stop. Una salida forzada de fin
    de datos solo se reclasifica si un stop dispara en esa misma barra o antes.
    """
    reasons = base_reason.copy()
    for i in range(entry_idx.shape[0]):
        reasons[i] = _classify_stop_reason(
            side=int(side[i]),
            entry_idx=int(entry_idx[i]),
            exit_idx=int(exit_idx[i]),
            raw_entry=float(inp.open[int(entry_idx[i])]),
            high=inp.high,
            low=inp.low,
            open_=inp.open,
            risk=risk,
            default_reason=int(base_reason[i]),
        )
    return reasons


def simulate(
    inp: FrameworkInput,
    costs: CostModel,
    size_fraction: float,
    initial_cash: float,
    risk: RiskModel | None = None,
) -> FrameworkResult:
    """Simula RF-5 con ``vectorbt.Portfolio.from_signals`` (plan §3.3, Enmienda G2b).

    - Desplaza las 4 señales una barra y descarta las entradas (de cualquier lado) que
      caerían en la última.
    - Ejecuta a ``open`` con ``slippage``/``fees`` como fracciones (``bps / 10 000``), sin
      acumular, ``size_fraction`` de la caja (``SizeType.Percent``). Ambos lados se pasan a
      VectorBT vía ``short_entries``/``short_exits`` (soporte nativo; sin
      ``direction="longonly"``). El núcleo ya garantiza (``position_to_signals``) que nunca
      hay una reversa directa ``+1↔-1`` sin pasar por ``0``, así que no hay señales
      simultáneas de ambos lados en la misma barra.
    - Liquidación final: si no hay salida por señal en la última barra, se fuerza una
      salida a su ``close`` en **ambos** lados (``exits[-1] = True`` y
      ``short_exits[-1] = True``); el lado sin posición abierta es un no-op para VectorBT.
      Si hay posición, su operación lleva ``exit_reason = 1`` (``end_of_data``).
    - ``return`` se recalcula como ``pnl / (entry_price·size + comisión de entrada)``
      (RF-6): el ``return`` de VectorBT divide solo por ``entry_price·size``.
    - ``risk`` (Enmienda G4): si no es ``None``, pasa ``open``/``high``/``low`` y
      ``sl_stop``/``tp_stop``/``sl_trail`` a VectorBT (detección intrabar nativa) y
      reclasifica cada salida por stop en ``stop_loss``/``take_profit`` (:func:`
      _stop_exit_reasons`). Con ``risk=None`` la llamada es idéntica a la previa.

    Raises:
        OutputOutOfRangeError: SI VectorBT deja alguna operación abierta al final.
    """
    n = inp.n_bars
    entries = _shift(inp.entries)
    exits = _shift(inp.exits)
    short_entries = _shift(inp.short_entries)
    short_exits = _shift(inp.short_exits)
    price = inp.open.copy()
    forced_exit = False
    if n:
        entries[-1] = False  # RF-5.1: una entrada en la última barra se ignora
        short_entries[-1] = False
        if not exits[-1] and not short_exits[-1]:
            # RF-5.6: liquidación final al cierre de la última barra, en ambos lados; el
            # lado sin posición abierta no genera operación (VectorBT la ignora).
            exits[-1] = True
            short_exits[-1] = True
            price[-1] = inp.close[-1]
            forced_exit = True

    stop_kwargs: dict[str, Any] = {}
    if risk is not None:
        # Enmienda G4: las mechas intrabar (``open``/``high``/``low``) solo alimentan la
        # detección de stops; la ejecución de señales sigue a ``price``. Sin ``risk`` no se
        # pasan, para preservar byte a byte la salida previa (goldens de las 17 estrategias).
        stop_kwargs = {
            "open": inp.open,
            "high": inp.high,
            "low": inp.low,
            "sl_stop": risk.sl_frac,
            "tp_stop": risk.tp_frac,
            "sl_trail": risk.sl_trail,
        }
    portfolio = vbt.Portfolio.from_signals(
        inp.close,
        entries,
        exits,
        short_entries=short_entries,
        short_exits=short_exits,
        price=price,
        fees=fee_rate(costs),
        slippage=slip(costs),
        size=size_fraction,
        size_type="percent",
        init_cash=initial_cash,
        accumulate=False,
        **stop_kwargs,
    )
    records = portfolio.trades.values
    if (records["status"] == _VBT_OPEN).any():
        msg = "output_out_of_range: trades: operación abierta tras la liquidación final"
        raise OutputOutOfRangeError(msg)
    records = records[np.argsort(records["entry_idx"], kind="stable")]

    exit_idx = records["exit_idx"]
    entry_idx = records["entry_idx"]
    entry_price, size, entry_fees = records["entry_price"], records["size"], records["entry_fees"]
    end_of_data = (exit_idx == n - 1) & forced_exit
    side = np.where(records["direction"] == _VBT_LONG, SIDE_LONG, SIDE_SHORT)
    base_reason = np.where(end_of_data, EXIT_REASON_END_OF_DATA, EXIT_REASON_SIGNAL)
    exit_reason = (
        _stop_exit_reasons(inp, entry_idx, exit_idx, side, base_reason, risk)
        if risk is not None
        else base_reason
    )
    equity = np.asarray(portfolio.value()).reshape(n)
    return FrameworkResult(
        entry_idx=entry_idx,
        exit_idx=exit_idx,
        side=side,
        entry_price=entry_price,
        exit_price=records["exit_price"],
        size=size,
        fees=entry_fees + records["exit_fees"],
        pnl=records["pnl"],
        return_=records["pnl"] / (entry_price * size + entry_fees),
        exit_reason=exit_reason,
        equity=equity,
        drawdown=_drawdown(equity, initial_cash),
    )


# --------------------------------------------------------------------------- punto 2


def _fail(detail: str) -> OutputOutOfRangeError:
    return OutputOutOfRangeError(f"output_out_of_range: {detail}")


def _check_domain(name: str, values: IntArray, domain: frozenset[int]) -> None:
    outside = sorted({int(v) for v in values} - domain)
    if outside:
        msg = f"trades: {name}: valores {outside} fuera del dominio {sorted(domain)}"
        raise _fail(msg)


def _check_positions(name: str, values: IntArray, n_bars: int) -> None:
    if bool(np.any(values < 0)) or bool(np.any(values >= n_bars)):
        msg = f"trades: {name}: índice fuera del rango [0, {n_bars})"
        raise _fail(msg)


def from_framework_output(res: FrameworkResult, open_time_ms: pl.Series) -> EngineOutput:
    """PUNTO DE CONVERSIÓN 2: doble precisión → ``Float32``/``Int64``/``Int8`` (RF-6).

    Tiempos por posición desde ``open_time_ms`` (``Int64`` original). La validación de
    dominio e índices precede al *downcast*; la de finitud se hace sobre los ``Float32``
    resultantes (un valor que desborda ``float32`` pasa a ``inf`` y se rechaza).

    Raises:
        OutputOutOfRangeError: SI hay ``NaN``/``inf``, ``side``/``exit_reason`` fuera de
            dominio, índices de barra fuera del rango o ``equity`` de otra longitud.
    """
    n_bars = open_time_ms.len()
    if res.equity.shape[0] != n_bars or res.drawdown.shape[0] != n_bars:
        msg = f"equity: {res.equity.shape[0]} filas != n_bars {n_bars}"
        raise _fail(msg)
    _check_domain("side", res.side, SIDE_DOMAIN)
    _check_domain("exit_reason", res.exit_reason, EXIT_REASON_DOMAIN)
    _check_positions("entry_idx", res.entry_idx, n_bars)
    _check_positions("exit_idx", res.exit_idx, n_bars)

    trade_columns: dict[str, Any] = {
        "entry_time_ms": open_time_ms.gather(pl.Series(res.entry_idx).cast(pl.UInt32)),
        "exit_time_ms": open_time_ms.gather(pl.Series(res.exit_idx).cast(pl.UInt32)),
        "side": res.side,
        "entry_price": res.entry_price,
        "exit_price": res.exit_price,
        "size": res.size,
        "fees": res.fees,
        "pnl": res.pnl,
        "return": res.return_,
        "exit_reason": res.exit_reason,
    }
    equity_columns: dict[str, Any] = {
        "open_time_ms": open_time_ms,
        "equity": res.equity,
        "drawdown": res.drawdown,
    }
    trades = pl.DataFrame(
        [pl.Series(name, trade_columns[name]).cast(dtype) for name, dtype in TRADES_SCHEMA.items()]
    )
    equity = pl.DataFrame(
        [pl.Series(name, equity_columns[name]).cast(dtype) for name, dtype in EQUITY_SCHEMA.items()]
    )
    output = EngineOutput(trades=trades, equity=equity)
    validate_engine_output(output, n_bars)
    return output


# --------------------------------------------------------------------------- motor


class VectorbtEngine:
    """``BacktestEngine`` sobre VectorBT: solo simula ``Signals`` ya calculadas (RF-1)."""

    name = ENGINE_NAME

    def version(self) -> str:
        """``vectorbt <versión instalada>`` (``engine_version``, RF-4)."""
        return f"vectorbt {vbt.__version__}"

    def run(  # noqa: PLR0913, PLR0917 - contrato de motor (RF-10); `risk` aditivo (Enmienda G4)
        self,
        candles: pl.DataFrame,
        signals: Signals,
        costs: CostModel,
        size_fraction: float,
        initial_cash: float,
        risk: RiskModel | None = None,
    ) -> EngineOutput:
        """Conversión de entrada → simulación → conversión de salida (plan §3.1).

        ``risk`` (Enmienda G4) aplica stops SL/TP/trailing a la corrida; ``None`` preserva
        el comportamiento previo sin stops.
        """
        inp = to_framework_input(candles, signals)
        res = simulate(inp, costs, size_fraction, initial_cash, risk)
        return from_framework_output(res, candles["open_time_ms"])
