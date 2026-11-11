"""Referencia de prueba independiente (B6.1, plan §5): señales, simulación y métricas.

Implementa con **bucles explícitos de Python en doble precisión** (permitida solo en
``tests/``, RFC-0004) la semántica normativa de RF-2 para ``sma_cross``,
``rsi_reversion`` y ``donchian_breakout``, el modelo de ejecución de RF-5, las tablas
canónicas de RF-6 y las fórmulas de RF-7. **No** llama al código de estrategias del
núcleo (``finances_core.backtest.strategies``) ni a ``metrics.compute``: es el contraste
independiente que fijan los *golden* y contra el que se comparan los motores (B6.2).

Convenciones que no fija RF-5 de forma literal (decisiones conservadoras, documentadas):

- ``equity`` de la última barra se valora **después** de la liquidación final
  (``end_of_data``), de modo que ``final_equity = initial_cash + Σ pnl``.
- ``exposure`` cuenta la barra de la liquidación final como expuesta (la posición sigue
  abierta al cierre de esa barra), igual que ``finances_core.backtest.metrics``.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import polars as pl
from finances_core.backtest.frames import (
    EQUITY_SCHEMA,
    EXIT_REASON_END_OF_DATA,
    EXIT_REASON_SIGNAL,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    SIDE_LONG,
    SIDE_SHORT,
    TRADES_SCHEMA,
)

BPS = 10_000.0
DAY_MS = 86_400_000
TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
TIMEFRAME_MS["1d"] = DAY_MS

Params = Mapping[str, Any]
SignalPair = tuple[list[bool], list[bool]]


def f32(value: float) -> float:
    """Redondea un ``float`` de Python al ``float32`` más cercano (y vuelve a doble)."""
    rounded: float = struct.unpack("<f", struct.pack("<f", value))[0]
    return rounded


@dataclass(frozen=True)
class Candles:
    """Velas RF-4 como tuplas de Python (valores ya representables en ``float32``)."""

    open_time_ms: tuple[int, ...]
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]

    @property
    def n(self) -> int:
        return len(self.close)

    def to_frame(self) -> pl.DataFrame:
        """``DataFrame`` canónico (``Int64`` / ``Float32``) que recibe el núcleo."""
        return pl.DataFrame(
            {
                "open_time_ms": pl.Series(self.open_time_ms, dtype=pl.Int64),
                "open": pl.Series(self.open, dtype=pl.Float32),
                "high": pl.Series(self.high, dtype=pl.Float32),
                "low": pl.Series(self.low, dtype=pl.Float32),
                "close": pl.Series(self.close, dtype=pl.Float32),
            }
        )


@dataclass(frozen=True)
class Costs:
    """Parámetros de ejecución de RF-5 (los de ``BacktestRequest``)."""

    initial_cash: float = 10_000.0
    size_fraction: float = 1.0
    fees_bps: float = 10.0
    slippage_bps: float = 0.0


# --------------------------------------------------------------------------- indicadores


def sma_series(close: Sequence[float], n: int) -> list[float | None]:
    """``SMA_n(t)`` = media de ``close[t-n+1..t]``; ``None`` para ``t < n-1``."""
    out: list[float | None] = []
    for t in range(len(close)):
        if t < n - 1:
            out.append(None)
            continue
        total = 0.0
        for k in range(t - n + 1, t + 1):
            total += close[k]
        out.append(total / n)
    return out


def _rsi_from(gain: float, loss: float) -> float:
    if loss == 0.0:
        return 100.0 if gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def rsi_series(close: Sequence[float], period: int) -> list[float | None]:
    """RSI de Wilder con semilla SMA de ``Δ[1..period]`` (RF-2); ``None`` para ``t < period``."""
    out: list[float | None] = [None] * len(close)
    avg_gain = avg_loss = 0.0
    for t in range(1, len(close)):
        delta = close[t] - close[t - 1]
        gain = delta if delta > 0.0 else 0.0
        loss = -delta if delta < 0.0 else 0.0
        if t <= period:
            avg_gain += gain
            avg_loss += loss
            if t == period:
                avg_gain /= period
                avg_loss /= period
                out[t] = _rsi_from(avg_gain, avg_loss)
            continue
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[t] = _rsi_from(avg_gain, avg_loss)
    return out


def donchian_bounds(
    high: Sequence[float], low: Sequence[float], window: int
) -> tuple[list[float | None], list[float | None]]:
    """``alto(t) = max(high[t-window..t-1])``, ``bajo(t) = min(low[t-window..t-1])``."""
    upper: list[float | None] = []
    lower: list[float | None] = []
    for t in range(len(high)):
        if t < window:
            upper.append(None)
            lower.append(None)
            continue
        hi = high[t - window]
        lo = low[t - window]
        for k in range(t - window + 1, t):
            hi = max(hi, high[k])
            lo = min(lo, low[k])
        upper.append(hi)
        lower.append(lo)
    return upper, lower


# --------------------------------------------------------------------------- señales


def _crosses(
    a: Sequence[float | None], b: Sequence[float | None], first: int
) -> tuple[list[bool], list[bool]]:
    """Cruces estrictos de ``a`` sobre ``b`` desde la barra ``first`` (exige ``t`` y ``t-1``)."""
    n = len(a)
    up = [False] * n
    down = [False] * n
    for t in range(max(first, 1), n):
        a0, a1, b0, b1 = a[t - 1], a[t], b[t - 1], b[t]
        if a0 is None or a1 is None or b0 is None or b1 is None:
            continue
        up[t] = a0 <= b0 and a1 > b1
        down[t] = a0 >= b0 and a1 < b1
    return up, down


def sma_cross_signals(close: Sequence[float], fast: int, slow: int) -> SignalPair:
    """RF-2 ``sma_cross``: cruces de ``SMA_fast`` sobre ``SMA_slow`` (``t >= slow``)."""
    return _crosses(sma_series(close, fast), sma_series(close, slow), slow)


def rsi_reversion_signals(
    close: Sequence[float], period: int, lower: float, upper: float
) -> SignalPair:
    """RF-2 ``rsi_reversion``: entra al cruzar ``lower`` a la baja, sale al cruzar ``upper``."""
    rsi = rsi_series(close, period)
    n = len(close)
    entries = [False] * n
    exits = [False] * n
    for t in range(period + 1, n):
        prev, now = rsi[t - 1], rsi[t]
        if prev is None or now is None:
            continue
        entries[t] = prev >= lower and now < lower
        exits[t] = prev <= upper and now > upper
    return entries, exits


def donchian_breakout_signals(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], window: int
) -> SignalPair:
    """RF-2 ``donchian_breakout``: ``close > alto`` / ``close < bajo`` (``t >= window``)."""
    upper, lower = donchian_bounds(high, low, window)
    n = len(close)
    entries = [False] * n
    exits = [False] * n
    for t in range(window, n):
        hi, lo = upper[t], lower[t]
        if hi is None or lo is None:
            continue
        entries[t] = close[t] > hi
        exits[t] = close[t] < lo
    return entries, exits


def reference_signals(kind: str, candles: Candles, params: Params) -> SignalPair:
    """Señales de RF-2 con la anulación de entrada y salida simultáneas de RF-16."""
    if kind == "sma_cross":
        entries, exits = sma_cross_signals(candles.close, int(params["fast"]), int(params["slow"]))
    elif kind == "rsi_reversion":
        entries, exits = rsi_reversion_signals(
            candles.close, int(params["period"]), float(params["lower"]), float(params["upper"])
        )
    elif kind == "donchian_breakout":
        entries, exits = donchian_breakout_signals(
            candles.high, candles.low, candles.close, int(params["window"])
        )
    else:
        raise ValueError(f"kind sin referencia: {kind}")
    both = [e and x for e, x in zip(entries, exits, strict=True)]
    return (
        [e and not b for e, b in zip(entries, both, strict=True)],
        [x and not b for x, b in zip(exits, both, strict=True)],
    )


def reference_position(entries: Sequence[bool], exits: Sequence[bool]) -> list[int]:
    """Posición-objetivo largo/plano sostenida a partir de eventos (RF-16, enmienda L0-G2a).

    Semantica de ``_lib.position_from_events``, reimplementada en Python puro para no
    depender del núcleo: entrada tiene prioridad, la posición se mantiene entre eventos y
    arranca plana (0).
    """
    pos: list[int] = []
    current = 0
    for enter, exit_ in zip(entries, exits, strict=True):
        if enter:
            current = 1
        elif exit_:
            current = 0
        pos.append(current)
    return pos


def position_edges(pos: Sequence[int]) -> SignalPair:
    """Flancos de la posición largo/plano: entra en +1 (``enter``) y deja +1 (``exit``)."""
    n = len(pos)
    enters = [False] * n
    exits = [False] * n
    prev = 0
    for t, value in enumerate(pos):
        enters[t] = value == 1 and prev != 1
        exits[t] = value != 1 and prev == 1
        prev = value
    return enters, exits


def threshold_margin(kind: str, candles: Candles, params: Params) -> tuple[float, int]:
    """Menor distancia relativa ``|a - b| / |b|`` de toda comparación de umbral evaluada.

    Devuelve ``(margen, barra)``. Un margen ≥ 1e-3 garantiza que ninguna comparación es
    un empate numérico que la recursión ``Float32`` del núcleo pueda resolver distinto.
    """
    best = (math.inf, -1)
    if kind == "sma_cross":
        fast = sma_series(candles.close, int(params["fast"]))
        slow = sma_series(candles.close, int(params["slow"]))
        pairs = [[(f, s)] for f, s in zip(fast, slow, strict=True)]
    elif kind == "rsi_reversion":
        lower, upper = float(params["lower"]), float(params["upper"])
        pairs = [[(r, lower), (r, upper)] for r in rsi_series(candles.close, int(params["period"]))]
    elif kind == "donchian_breakout":
        hi, lo = donchian_bounds(candles.high, candles.low, int(params["window"]))
        pairs = [[(c, h), (c, low)] for c, h, low in zip(candles.close, hi, lo, strict=True)]
    else:
        raise ValueError(f"kind sin referencia: {kind}")
    for t, comparisons in enumerate(pairs):
        for a, b in comparisons:
            if a is None or b is None:
                continue
            margin = abs(a - b) / abs(b)
            if margin < best[0]:
                best = (margin, t)
    return best


# --------------------------------------------------------------------------- simulación


@dataclass(frozen=True)
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    size: float
    fees: float
    pnl: float
    ret: float
    exit_reason: int


@dataclass(frozen=True)
class Simulation:
    trades: tuple[Trade, ...]
    equity: tuple[float, ...]
    drawdown: tuple[float, ...]
    exposed_bars: int


def simulate(
    candles: Candles, entries: Sequence[bool], exits: Sequence[bool], costs: Costs
) -> Simulation:
    """Modelo de ejecución de RF-5 con un bucle barra a barra (largo/plano).

    La señal al cierre de ``t`` se ejecuta en la apertura de ``t+1``; una entrada que se
    ejecutaría en la última barra se ignora; la posición abierta al final se liquida al
    cierre de la última barra (``exit_reason = 1``).
    """
    n = candles.n
    fee = costs.fees_bps / BPS
    slip = costs.slippage_bps / BPS
    cash = costs.initial_cash
    qty = 0.0
    entry_price = entry_fee = 0.0
    entry_time = 0
    pending: str | None = None
    trades: list[Trade] = []
    equity: list[float] = []
    exposed = 0

    def close_trade(price: float, time_ms: int, reason: int) -> None:
        nonlocal cash, qty
        exit_fee = fee * price * qty
        cash += price * qty - exit_fee
        fees = entry_fee + exit_fee
        pnl = (price - entry_price) * qty - fees
        ret = pnl / (entry_price * qty + entry_fee)
        trades.append(Trade(entry_time, time_ms, entry_price, price, qty, fees, pnl, ret, reason))
        qty = 0.0

    for t in range(n):
        if pending == "buy" and t < n - 1:
            entry_price = candles.open[t] * (1.0 + slip)
            qty = costs.size_fraction * cash / (entry_price * (1.0 + fee))
            entry_fee = fee * entry_price * qty
            cash -= entry_price * qty + entry_fee
            entry_time = candles.open_time_ms[t]
        elif pending == "sell":
            close_trade(candles.open[t] * (1.0 - slip), candles.open_time_ms[t], EXIT_REASON_SIGNAL)
        pending = None
        if qty > 0.0:
            exposed += 1
            if t == n - 1:
                close_trade(
                    candles.close[t] * (1.0 - slip),
                    candles.open_time_ms[t],
                    EXIT_REASON_END_OF_DATA,
                )
        equity.append(cash + qty * candles.close[t])
        if qty == 0.0 and entries[t]:
            pending = "buy"
        elif qty > 0.0 and exits[t]:
            pending = "sell"

    peak = costs.initial_cash
    drawdown: list[float] = []
    for value in equity:
        peak = max(peak, value)
        drawdown.append(value / peak - 1.0)
    return Simulation(tuple(trades), tuple(equity), tuple(drawdown), exposed)


def trades_frame(sim: Simulation) -> pl.DataFrame:
    """Tabla ``trades`` canónica (RF-6) con los dtypes de ``TRADES_SCHEMA``."""
    rows = sim.trades
    data: dict[str, list[Any]] = {
        "entry_time_ms": [r.entry_time_ms for r in rows],
        "exit_time_ms": [r.exit_time_ms for r in rows],
        "side": [SIDE_LONG for _ in rows],
        "entry_price": [r.entry_price for r in rows],
        "exit_price": [r.exit_price for r in rows],
        "size": [r.size for r in rows],
        "fees": [r.fees for r in rows],
        "pnl": [r.pnl for r in rows],
        "return": [r.ret for r in rows],
        "exit_reason": [r.exit_reason for r in rows],
    }
    return pl.DataFrame(
        {name: pl.Series(name, data[name], dtype=dtype) for name, dtype in TRADES_SCHEMA.items()}
    )


def equity_frame(candles: Candles, sim: Simulation) -> pl.DataFrame:
    """Tabla ``equity`` canónica (RF-6) con los dtypes de ``EQUITY_SCHEMA``."""
    data: dict[str, Sequence[Any]] = {
        "open_time_ms": candles.open_time_ms,
        "equity": sim.equity,
        "drawdown": sim.drawdown,
    }
    return pl.DataFrame(
        {name: pl.Series(name, data[name], dtype=dtype) for name, dtype in EQUITY_SCHEMA.items()}
    )


# --------------------------------------------------------------------------- métricas


def summarize(
    sim: Simulation, candles: Candles, *, initial_cash: float, timeframe: str
) -> dict[str, Any]:
    """``summary.json`` de RF-7: métricas en doble precisión redondeadas a ``float32``.

    Incluye el rango efectivo (``start_ms``/``end_ms``) y ``warnings`` (vacía: el fixture
    no tiene huecos ni historial insuficiente).
    """
    equity = sim.equity
    n = len(equity)
    periods = 365.25 * DAY_MS / TIMEFRAME_MS[timeframe]
    returns: list[float] = []
    previous = initial_cash
    for value in equity:
        returns.append(value / previous - 1.0)
        previous = value
    mean = sum(returns) / n
    sharpe: float | None = None
    if n >= 2:  # noqa: PLR2004 - ddof = 1 exige al menos dos barras (RF-7)
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        if variance > 0.0:
            sharpe = mean / math.sqrt(variance) * math.sqrt(periods)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / n)
    sortino = mean / downside * math.sqrt(periods) if downside > 0.0 else None
    growth = equity[-1] / initial_cash
    cagr: float | None
    if equity[-1] <= 0.0:
        cagr = -1.0
    else:
        try:
            cagr = growth ** (periods / n) - 1.0
        except OverflowError:
            cagr = None
        if cagr is not None and not math.isfinite(cagr):
            cagr = None
    n_trades = len(sim.trades)
    wins = sum(1 for trade in sim.trades if trade.pnl > 0.0)

    def opt(value: float | None) -> float | None:
        return None if value is None else f32(value)

    return {
        "total_return": f32(growth - 1.0),
        "cagr": opt(cagr),
        "max_drawdown": f32(min(sim.drawdown)),
        "sharpe": opt(sharpe),
        "sortino": opt(sortino),
        "win_rate": opt(wins / n_trades if n_trades else None),
        "n_trades": n_trades,
        "exposure": f32(sim.exposed_bars / n),
        "initial_cash": initial_cash,
        "final_equity": f32(equity[-1]),
        "n_bars": n,
        "start_ms": candles.open_time_ms[0],
        "end_ms": candles.open_time_ms[-1],
        "warnings": [],
    }


# --------------------------------------------------------------------- corto (Enmienda G2b)


@dataclass(frozen=True)
class ShortRoundTrip:
    """Resultado de una operación corta aislada (``simulate_short_round_trip``)."""

    side: int
    entry_price: float
    exit_price: float
    size: float
    fees: float
    pnl: float
    return_: float


def simulate_short_round_trip(entry_open: float, exit_open: float, costs: Costs) -> ShortRoundTrip:
    """Economía de UNA operación corta aislada, en Python puro (sin VectorBT/NumPy).

    Vender para abrir el corto sufre el mismo deslizamiento que una venta de cierre largo
    (``entry_price = entry_open·(1 - s)``, recibe menos); comprar para cerrarlo sufre el
    mismo deslizamiento que una compra de apertura larga (``exit_price = exit_open·(1 + s)``,
    paga más): el deslizamiento siempre perjudica al operador, en ambos lados (RF-5, Enmienda
    G2b). ``size`` dimensiona con toda la caja inicial (``size_fraction = 1``) igual que
    VectorBT (``SizeType.Percent``): ``size = initial_cash / (entry_price·(1 + fee))``.
    ``pnl = (entry_price - exit_price)·size - fees``; ``fees`` cobra la tasa sobre el nocional
    de cada pata (entrada y salida). Sirve como oráculo independiente para un caso de
    contrato con cortos (Verification rule, ítem de riesgo numérico/dominio).
    """
    s = costs.slippage_bps / BPS
    fee = costs.fees_bps / BPS
    entry_price = entry_open * (1.0 - s)
    exit_price = exit_open * (1.0 + s)
    size = costs.initial_cash / (entry_price * (1.0 + fee))
    entry_fees = entry_price * size * fee
    exit_fees = exit_price * size * fee
    fees = entry_fees + exit_fees
    pnl = (entry_price - exit_price) * size - fees
    return_ = pnl / (entry_price * size + entry_fees)
    return ShortRoundTrip(
        side=SIDE_SHORT,
        entry_price=entry_price,
        exit_price=exit_price,
        size=size,
        fees=fees,
        pnl=pnl,
        return_=return_,
    )


# ------------------------------------------------- stops de riesgo (Enmienda G4)


@dataclass(frozen=True)
class StopTrade:
    """Operación cerrada bajo stops (``simulate_with_stops``); ``side`` largo/corto."""

    entry_time_ms: int
    exit_time_ms: int
    side: int
    entry_price: float
    exit_price: float
    size: float
    fees: float
    pnl: float
    ret: float
    exit_reason: int


@dataclass(frozen=True)
class StopSim:
    """Salida de :func:`simulate_with_stops`: operaciones, ``equity`` y ``drawdown``."""

    trades: tuple[StopTrade, ...]
    equity: tuple[float, ...]
    drawdown: tuple[float, ...]
    exposed_bars: int


def _touches(
    *, side: int, bar_open: float, bar_high: float, bar_low: float, level: float, is_stop_loss: bool
) -> bool:
    """¿La vela alcanza ``level`` en la dirección de ese stop (incluye hueco al ``open``)?

    Stop-loss largo / take-profit corto miran hacia abajo (``low``/``open`` ≤ ``level``);
    take-profit largo / stop-loss corto miran hacia arriba (``high``/``open`` ≥ ``level``).
    """
    looks_down = (side == SIDE_LONG) == is_stop_loss
    if looks_down:
        return bar_low <= level or bar_open <= level
    return bar_high >= level or bar_open >= level


def _stop_fill(*, bar_open: float, level: float, looks_down: bool) -> float:
    """Precio de llenado del stop: el ``open`` si la barra abrió pasado el nivel (hueco), si
    no el nivel exacto. Los stops no sufren deslizamiento (verificado contra VectorBT).
    """
    if looks_down:
        return bar_open if bar_open <= level else level
    return bar_open if bar_open >= level else level


def _bar_stop_exit(
    *,
    side: int,
    raw_entry: float,
    extreme: float,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    risk: Any,
) -> tuple[float, int] | None:
    """``(exit_price, reason)`` si un stop dispara en esta barra, si no ``None``.

    Chequea primero el stop-loss (prioridad en empate), luego el take-profit. El nivel del
    stop-loss es fijo desde ``raw_entry`` o, con ``sl_trail``, desde ``extreme`` (pico/valle
    de las barras previas).
    """
    sign = 1.0 if side == SIDE_LONG else -1.0
    sl_down = side == SIDE_LONG
    touch = {"side": side, "bar_open": bar_open, "bar_high": bar_high, "bar_low": bar_low}
    if risk.sl_frac is not None:
        basis = extreme if risk.sl_trail else raw_entry
        sl_level = basis * (1.0 - sign * risk.sl_frac)
        if _touches(level=sl_level, is_stop_loss=True, **touch):
            return _stop_fill(bar_open=bar_open, level=sl_level, looks_down=sl_down), (
                EXIT_REASON_STOP_LOSS
            )
    if risk.tp_frac is not None:
        tp_level = raw_entry * (1.0 + sign * risk.tp_frac)
        if _touches(level=tp_level, is_stop_loss=False, **touch):
            return _stop_fill(bar_open=bar_open, level=tp_level, looks_down=not sl_down), (
                EXIT_REASON_TAKE_PROFIT
            )
    return None


def simulate_with_stops(  # noqa: PLR0912, PLR0915 - oráculo lineal de 5 pasos por barra
    candles: Candles,
    entries: Sequence[bool],
    exits: Sequence[bool],
    entries_short: Sequence[bool],
    exits_short: Sequence[bool],
    costs: Costs,
    risk: Any,
) -> StopSim:
    """Oráculo independiente de RF-5 + stops de riesgo (Enmienda G4), en Python puro.

    Una posición a la vez (largo o corto). Señal al cierre de ``t`` → orden en la apertura
    de ``t+1``; entrada en la última barra ignorada; posición abierta al final liquidada al
    ``close`` (``end_of_data``). Los stops se evalúan en las barras **posteriores** a la de
    entrada contra ``high``/``low`` (con hueco al ``open``): nivel fijo desde ``raw_entry``
    (``open`` de la barra de entrada, pre-deslizamiento), o trailing desde el pico/valle de
    las barras previas (sin la actual ni la de entrada). En empate en la misma barra gana el
    stop-loss. Entrada y salida por señal/fin de datos sufren deslizamiento; los stops no.
    """
    n = candles.n
    fee = costs.fees_bps / BPS
    slip = costs.slippage_bps / BPS
    sl_frac = getattr(risk, "sl_frac", None) if risk is not None else None
    trail = bool(getattr(risk, "sl_trail", False)) if risk is not None else False

    cash = costs.initial_cash
    qty = 0.0
    side = 0
    entry_price = entry_fee = raw_entry = 0.0
    entry_time = entry_bar = 0
    extreme = 0.0  # pico (largo) / valle (corto) del trailing, sin la barra actual
    pending: str | None = None
    trades: list[StopTrade] = []
    equity: list[float] = []
    exposed = 0

    def record(exit_price: float, time_ms: int, reason: int) -> None:
        nonlocal cash, qty, side
        exit_fee = fee * exit_price * qty
        if side == SIDE_LONG:
            cash += exit_price * qty - exit_fee
            pnl = (exit_price - entry_price) * qty - (entry_fee + exit_fee)
        else:
            cash -= exit_price * qty + exit_fee
            pnl = (entry_price - exit_price) * qty - (entry_fee + exit_fee)
        fees = entry_fee + exit_fee
        ret = pnl / (entry_price * qty + entry_fee)
        trades.append(
            StopTrade(
                entry_time, time_ms, side, entry_price, exit_price, qty, fees, pnl, ret, reason
            )
        )
        qty = 0.0
        side = 0

    for t in range(n):
        # 1) Ejecutar la orden pendiente a la apertura de t (deslizamiento en señales).
        if pending == "long" and t < n - 1:
            raw_entry = candles.open[t]
            entry_price = raw_entry * (1.0 + slip)
            qty = costs.size_fraction * cash / (entry_price * (1.0 + fee))
            entry_fee = fee * entry_price * qty
            cash -= entry_price * qty + entry_fee
            side, entry_time, entry_bar, extreme = SIDE_LONG, candles.open_time_ms[t], t, raw_entry
        elif pending == "short" and t < n - 1:
            raw_entry = candles.open[t]
            entry_price = raw_entry * (1.0 - slip)
            qty = costs.size_fraction * cash / (entry_price * (1.0 + fee))
            entry_fee = fee * entry_price * qty
            cash += entry_price * qty - entry_fee
            side, entry_time, entry_bar, extreme = SIDE_SHORT, candles.open_time_ms[t], t, raw_entry
        elif pending == "exit_long" and side == SIDE_LONG:
            record(candles.open[t] * (1.0 - slip), candles.open_time_ms[t], EXIT_REASON_SIGNAL)
        elif pending == "exit_short" and side == SIDE_SHORT:
            record(candles.open[t] * (1.0 + slip), candles.open_time_ms[t], EXIT_REASON_SIGNAL)
        pending = None

        # 2) Stops: solo en barras posteriores a la de entrada (la entrada se excluye).
        if side != 0 and t > entry_bar and risk is not None:
            hit = _bar_stop_exit(
                side=side,
                raw_entry=raw_entry,
                extreme=extreme,
                bar_open=candles.open[t],
                bar_high=candles.high[t],
                bar_low=candles.low[t],
                risk=risk,
            )
            if hit is not None:
                record(hit[0], candles.open_time_ms[t], hit[1])
            elif trail and sl_frac is not None:
                # Pico/valle para la próxima barra (la actual se excluye del nivel vigente).
                extreme = (
                    max(extreme, candles.high[t])
                    if side == SIDE_LONG
                    else min(extreme, candles.low[t])
                )

        # 3) Liquidación final al cierre de la última barra.
        if side != 0 and t == n - 1:
            exit_px = candles.close[t] * (1.0 - slip if side == SIDE_LONG else 1.0 + slip)
            record(exit_px, candles.open_time_ms[t], EXIT_REASON_END_OF_DATA)

        # 4) Marca de equity y exposición.
        if side == SIDE_LONG:
            exposed += 1
            equity.append(cash + qty * candles.close[t])
        elif side == SIDE_SHORT:
            exposed += 1
            equity.append(cash - qty * candles.close[t])
        else:
            equity.append(cash)

        # 5) Programar la orden de la próxima barra (señal al cierre de t).
        if side == 0 and entries[t]:
            pending = "long"
        elif side == 0 and entries_short[t]:
            pending = "short"
        elif side == SIDE_LONG and exits[t]:
            pending = "exit_long"
        elif side == SIDE_SHORT and exits_short[t]:
            pending = "exit_short"

    peak = costs.initial_cash
    drawdown: list[float] = []
    for value in equity:
        peak = max(peak, value)
        drawdown.append(value / peak - 1.0)
    return StopSim(tuple(trades), tuple(equity), tuple(drawdown), exposed)


def stops_trades_frame(sim: StopSim) -> pl.DataFrame:
    """Tabla ``trades`` canónica (RF-6) de un :class:`StopSim` (con ``side``)."""
    rows = sim.trades
    data: dict[str, list[Any]] = {
        "entry_time_ms": [r.entry_time_ms for r in rows],
        "exit_time_ms": [r.exit_time_ms for r in rows],
        "side": [r.side for r in rows],
        "entry_price": [r.entry_price for r in rows],
        "exit_price": [r.exit_price for r in rows],
        "size": [r.size for r in rows],
        "fees": [r.fees for r in rows],
        "pnl": [r.pnl for r in rows],
        "return": [r.ret for r in rows],
        "exit_reason": [r.exit_reason for r in rows],
    }
    return pl.DataFrame(
        {name: pl.Series(name, data[name], dtype=dtype) for name, dtype in TRADES_SCHEMA.items()}
    )
