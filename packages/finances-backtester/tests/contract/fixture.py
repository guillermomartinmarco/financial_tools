"""Fixture determinista de la suite de contrato (B6.1, plan §5).

``FIXTURE_BARS`` velas ``1h`` generadas con PCG64 (el generador en Python puro de
``finances_core.backtest.strategies.testing``) y semilla fija, en ocho tramos alternos de
tendencia (alcista/bajista) y rango (reversión a un nivel). Cada precio se redondea a
``float32`` al generarse, de modo que el núcleo (``Float32``) y la referencia (doble
precisión) leen exactamente los mismos valores.

**Margen mínimo en los umbrales**: un cierre candidato que deje alguna comparación de
umbral de las tres estrategias (con ``GOLDEN_PARAMS``) a menos de ``GENERATION_MARGIN``
relativo se descarta y se vuelve a muestrear con el mismo generador. Así ningún cruce es
un empate numérico y las señales ``Float32`` del núcleo coinciden bit a bit con las de la
referencia en doble precisión (A6). La prueba independiente de ese margen la hace
``reference.threshold_margin`` sobre la serie completa (umbral ``TIE_MARGIN``).

``sma_cross`` usa ``fast = 5``, ``slow = 20`` y no los valores por defecto (20/100): con
20/100 la banda de exclusión en el espacio del cierre (``margen·SMA / (1/fast - 1/slow)``)
mide ≈ 5 % del precio y obligaría a un ruido por barra irreal; con 5/20 mide ≈ 2 %.
``rsi_reversion`` usa ``period = 10`` (por defecto 14): con 14 y los umbrales por defecto
30/70 el fixture solo produce 3 operaciones (< 5 exigidas por tasks.md B6.1).
``donchian_breakout`` usa su valor por defecto (``window = 20``).
"""

from __future__ import annotations

import functools
import hashlib
import struct
from typing import Any

from finances_core.backtest.strategies.testing import _Pcg64

from tests.contract.reference import Candles, Costs, f32

FIXTURE_SEED = 20_261_103
FIXTURE_BARS = 2_400
TIMEFRAME = "1h"
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
HOUR_MS = 3_600_000
SEGMENTS = 8
TREND_DRIFT = 0.002
NOISE = 0.02  # amplitud del ruido uniforme por barra: ±2 %
WICK = 0.004
REVERSION = 0.05
MAX_ATTEMPTS = 10_000

#: Umbral de empate verificado por la prueba (tasks.md B6.1: 1e-3 relativo).
TIE_MARGIN = 1e-3
#: Margen exigido al generar (holgura sobre ``TIE_MARGIN``).
GENERATION_MARGIN = 1.5e-3

KINDS = ("sma_cross", "rsi_reversion", "donchian_breakout")
GOLDEN_PARAMS: dict[str, dict[str, Any]] = {
    "sma_cross": {"fast": 5, "slow": 20},
    "rsi_reversion": {"period": 10, "lower": 30.0, "upper": 70.0},
    "donchian_breakout": {"window": 20},
}
#: Costes de los *golden*: valores por defecto de ``BacktestRequest`` con deslizamiento
#: distinto de cero para ejercitar RF-5 regla 3.
GOLDEN_COSTS = Costs(initial_cash=10_000.0, size_fraction=1.0, fees_bps=10.0, slippage_bps=5.0)


class _Margins:
    """Estado incremental (doble precisión) de las comparaciones de umbral por barra."""

    def __init__(self) -> None:
        sma = GOLDEN_PARAMS["sma_cross"]
        rsi = GOLDEN_PARAMS["rsi_reversion"]
        self.fast, self.slow = int(sma["fast"]), int(sma["slow"])
        self.period = int(rsi["period"])
        self.lower, self.upper = float(rsi["lower"]), float(rsi["upper"])
        self.window = int(GOLDEN_PARAMS["donchian_breakout"]["window"])
        self.close: list[float] = []
        self.high: list[float] = []
        self.low: list[float] = []
        self.gain = 0.0
        self.loss = 0.0

    def _wilder(self, close: float) -> tuple[float, float] | None:
        t = len(self.close)
        if t == 0:
            return None
        delta = close - self.close[-1]
        gain, loss = max(delta, 0.0), max(-delta, 0.0)
        if t < self.period:
            return self.gain + gain, self.loss + loss
        if t == self.period:
            return (self.gain + gain) / self.period, (self.loss + loss) / self.period
        p = self.period
        return (self.gain * (p - 1) + gain) / p, (self.loss * (p - 1) + loss) / p

    def margin(self, close: float) -> float:
        """Menor margen relativo de las comparaciones que se evalúan con este cierre."""
        t = len(self.close)
        found = [1.0]
        if t >= self.slow - 1:
            window = [*self.close[t - self.slow + 1 :], close]
            fast = sum(window[-self.fast :]) / self.fast
            slow = sum(window) / self.slow
            found.append(abs(fast - slow) / slow)
        state = self._wilder(close)
        if state is not None and t >= self.period:
            gain, loss = state
            rsi = 100.0 if loss == 0.0 and gain > 0.0 else 50.0
            if loss != 0.0:
                rsi = 100.0 - 100.0 / (1.0 + gain / loss)
            found.extend((abs(rsi - self.lower) / self.lower, abs(rsi - self.upper) / self.upper))
        if t >= self.window:
            hi = max(self.high[t - self.window :])
            lo = min(self.low[t - self.window :])
            found.extend((abs(close - hi) / hi, abs(close - lo) / lo))
        return min(found)

    def commit(self, close: float, high: float, low: float) -> None:
        state = self._wilder(close)
        if state is not None:
            self.gain, self.loss = state
        self.close.append(close)
        self.high.append(high)
        self.low.append(low)


def build_fixture(n: int = FIXTURE_BARS, seed: int = FIXTURE_SEED) -> Candles:
    """Genera el fixture (determinista: misma semilla ⇒ mismas velas)."""
    rng = _Pcg64(seed)
    margins = _Margins()
    segment_len = n // SEGMENTS
    previous = level = 100.0
    rows: dict[str, list[float]] = {name: [] for name in ("open", "high", "low", "close")}
    for t in range(n):
        segment = min(t // segment_len, SEGMENTS - 1)
        if segment % 2:
            drift = -REVERSION * (previous / level - 1.0)
        else:
            drift = TREND_DRIFT if segment % 4 == 0 else -TREND_DRIFT
            level = previous
        for _ in range(MAX_ATTEMPTS):
            close = f32(previous * (1.0 + drift + (rng.uniform() - 0.5) * 2.0 * NOISE))
            if margins.margin(close) >= GENERATION_MARGIN:
                break
        else:
            raise RuntimeError(f"fixture: sin cierre con margen suficiente en t={t}")
        high = f32(max(previous, close) * (1.0 + rng.uniform() * WICK))
        low = f32(min(previous, close) * (1.0 - rng.uniform() * WICK))
        margins.commit(close, high, low)
        rows["open"].append(previous)
        rows["high"].append(high)
        rows["low"].append(low)
        rows["close"].append(close)
        previous = close
    return Candles(
        open_time_ms=tuple(START_MS + t * HOUR_MS for t in range(n)),
        open=tuple(rows["open"]),
        high=tuple(rows["high"]),
        low=tuple(rows["low"]),
        close=tuple(rows["close"]),
    )


@functools.cache
def fixture_candles() -> Candles:
    """Fixture de la suite, generado una vez por proceso."""
    return build_fixture()


def candles_sha256(candles: Candles) -> str:
    """Huella SHA-256 de las velas (``open_time_ms`` ``<q``, precios ``<f``, por barra)."""
    digest = hashlib.sha256()
    for t in range(candles.n):
        digest.update(struct.pack("<q", candles.open_time_ms[t]))
        digest.update(
            struct.pack("<4f", candles.open[t], candles.high[t], candles.low[t], candles.close[t])
        )
    return digest.hexdigest()
