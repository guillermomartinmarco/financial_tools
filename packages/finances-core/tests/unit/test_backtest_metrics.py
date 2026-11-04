"""B3.2: métricas del resumen (RF-7, A8) con series calculadas a mano y prueba de precisión.

Los valores esperados son literales calculados a mano (o con calculadora en doble precisión),
nunca llamando a la implementación. La referencia de precisión (``_reference``) usa doble
precisión de Python y solo vive en ``tests/`` (RFC-0004).
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

import polars as pl
import pytest

from finances_core.backtest.frames import EQUITY_SCHEMA, TRADES_SCHEMA
from finances_core.backtest.metrics import compute, periods_per_year
from finances_core.schemas.backtest import RunSummary

DAY_MS = 86_400_000
MINUTE_MS = 60_000
REL = 1e-4


def _equity(values: list[float], drawdown: list[float], step_ms: int = DAY_MS) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open_time_ms": [i * step_ms for i in range(len(values))],
            "equity": values,
            "drawdown": drawdown,
        },
        schema=EQUITY_SCHEMA,
    )


def _trades(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=TRADES_SCHEMA)


def _trade(entry_bar: int, exit_bar: int, pnl: float, reason: int) -> dict[str, Any]:
    return {
        "entry_time_ms": entry_bar * DAY_MS,
        "exit_time_ms": exit_bar * DAY_MS,
        "side": 1,
        "entry_price": 100.0,
        "exit_price": 100.0,
        "size": 1.0,
        "fees": 0.0,
        "pnl": pnl,
        "return": pnl / 100.0,
        "exit_reason": reason,
    }


#: 5 barras ``1d``, ``initial_cash = 100``: r = [0,1, -0,1, 0, 0,1, 0,1].
#: Máximo acumulado (incluye 100): [110, 110, 110, 110, 119,79].
HAND_EQUITY = _equity(
    [110.0, 99.0, 99.0, 108.9, 119.79],
    [0.0, -0.1, -0.1, -0.01, 0.0],
)
#: Operación ganadora con salida por señal en la barra 2 (expuesta en las barras 0 y 1) y
#: perdedora liquidada al final de los datos en la barra 4 (expuesta en las barras 3 y 4).
HAND_TRADES = _trades([_trade(0, 2, 10.0, 0), _trade(3, 4, -1.0, 1)])


def _hand_summary() -> RunSummary:
    return compute(HAND_TRADES, HAND_EQUITY, initial_cash=100.0, timeframe="1d")


def test_periods_per_year_for_1m_1h_1d() -> None:
    # P = 365,25 · 86 400 000 / tf_ms (A8).
    assert periods_per_year("1m") == 525_960.0
    assert periods_per_year("1h") == 8_766.0
    assert periods_per_year("1d") == 365.25


def test_periods_per_year_rejects_unknown_timeframe() -> None:
    with pytest.raises(ValueError, match="timeframe"):
        periods_per_year("7x")


def test_total_return_and_final_equity() -> None:
    summary = _hand_summary()
    # 119,79 / 100 - 1
    assert summary.total_return == pytest.approx(0.1979, rel=REL)
    assert summary.final_equity == pytest.approx(119.79, rel=1e-6)
    assert summary.initial_cash == 100.0
    assert summary.n_bars == 5


def test_cagr() -> None:
    # 1,1979 ^ (365,25 / 5) - 1
    assert _hand_summary().cagr == pytest.approx(535_329.8443029559, rel=REL)


def test_max_drawdown() -> None:
    assert _hand_summary().max_drawdown == pytest.approx(-0.1, rel=REL)


def test_sharpe() -> None:
    # media 0,04; desv (ddof=1) √0,008; · √365,25
    assert _hand_summary().sharpe == pytest.approx(8.546929273136639, rel=REL)


def test_sortino() -> None:
    # media 0,04; √media(min(r,0)²) = √0,002; · √365,25
    assert _hand_summary().sortino == pytest.approx(17.093858546273275, rel=REL)


def test_win_rate_n_trades_and_exposure() -> None:
    summary = _hand_summary()
    assert summary.n_trades == 2
    assert summary.win_rate == pytest.approx(0.5)
    # barras 0, 1 (salida por señal en la apertura de la 2) y 3, 4 (liquidación al cierre).
    assert summary.exposure == pytest.approx(0.8)


def test_flat_equity_gives_null_sharpe_and_sortino_and_null_win_rate() -> None:
    equity = _equity([100.0] * 6, [0.0] * 6)
    summary = compute(_trades([]), equity, initial_cash=100.0, timeframe="1d")
    assert summary.sharpe is None
    assert summary.sortino is None
    assert summary.win_rate is None
    assert summary.n_trades == 0
    assert summary.exposure == 0.0
    assert summary.total_return == 0.0
    assert summary.cagr == 0.0
    assert summary.max_drawdown == 0.0


def test_only_gains_gives_null_sortino_but_sharpe() -> None:
    # r = [0,1, 0, 0,1]: media 0,0666…, desv 0,057735…; sin pérdidas → sortino null.
    equity = _equity([110.0, 110.0, 121.0], [0.0, 0.0, 0.0])
    summary = compute(_trades([]), equity, initial_cash=100.0, timeframe="1d")
    assert summary.sortino is None
    assert summary.sharpe == pytest.approx(22.068076490713914, rel=REL)


def test_single_bar_gives_null_sharpe() -> None:
    equity = _equity([90.0], [-0.1])
    summary = compute(_trades([]), equity, initial_cash=100.0, timeframe="1d")
    assert summary.sharpe is None
    # r = [-0,1]: media -0,1 / √0,01 · √365,25
    assert summary.sortino == pytest.approx(-19.11151485361639, rel=REL)
    assert summary.total_return == pytest.approx(-0.1, rel=REL)


def test_cagr_is_minus_one_when_equity_is_wiped_out() -> None:
    equity = _equity([50.0, 0.0], [-0.5, -1.0])
    summary = compute(_trades([]), equity, initial_cash=100.0, timeframe="1d")
    assert summary.cagr == -1.0
    assert summary.total_return == -1.0


def test_rejects_empty_equity() -> None:
    with pytest.raises(ValueError, match="n_bars"):
        compute(_trades([]), _equity([], []), initial_cash=100.0, timeframe="1d")


# ---------------------------------------------------------------- precisión (RFC-0004)

N_PRECISION = 1_000_000
PRECISION_TOL = 1e-3


def _precision_series() -> tuple[pl.DataFrame, pl.DataFrame, float]:
    rng = random.Random(20261103)
    initial_cash = 10_000.0
    returns = pl.Series([rng.gauss(2e-6, 1e-3) for _ in range(N_PRECISION)], dtype=pl.Float32)
    equity_values = ((returns + 1).cum_prod() * initial_cash).cast(pl.Float32)
    peak = equity_values.cum_max().clip(lower_bound=initial_cash)
    equity = pl.DataFrame(
        {
            "open_time_ms": pl.int_range(N_PRECISION, eager=True) * MINUTE_MS,
            "equity": equity_values,
            "drawdown": (equity_values / peak - 1).cast(pl.Float32),
        },
        schema=EQUITY_SCHEMA,
    )
    rows = [
        {
            "entry_time_ms": start * MINUTE_MS,
            "exit_time_ms": (start + 500) * MINUTE_MS,
            "side": 1,
            "entry_price": 1.0,
            "exit_price": 1.0,
            "size": 1.0,
            "fees": 0.0,
            "pnl": rng.uniform(-1.0, 1.0),
            "return": 0.0,
            "exit_reason": 0,
        }
        for start in range(0, N_PRECISION - 1_000, 2_000)
    ]
    return _trades(rows), equity, initial_cash


def _reference(
    trades: pl.DataFrame, equity: pl.DataFrame, initial_cash: float, periods: float
) -> dict[str, float]:
    """RF-7 en doble precisión con sumas exactas (``math.fsum``), independiente del núcleo."""
    values = equity["equity"].to_list()
    previous = [initial_cash, *values[:-1]]
    r = [v / p - 1.0 for v, p in zip(values, previous, strict=True)]
    n = len(r)
    mean = math.fsum(r) / n
    std = math.sqrt(math.fsum((x - mean) ** 2 for x in r) / (n - 1))
    downside = math.sqrt(math.fsum(min(x, 0.0) ** 2 for x in r) / n)
    peak, max_dd = initial_cash, 0.0
    for v in values:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1.0)
    pnl = trades["pnl"].to_list()
    exposed = sum(
        (exit_ms - entry_ms) // MINUTE_MS
        for entry_ms, exit_ms in zip(
            trades["entry_time_ms"].to_list(), trades["exit_time_ms"].to_list(), strict=True
        )
    )
    return {
        "total_return": values[-1] / initial_cash - 1.0,
        "cagr": (values[-1] / initial_cash) ** (periods / n) - 1.0,
        "max_drawdown": max_dd,
        "sharpe": mean / std * math.sqrt(periods),
        "sortino": mean / downside * math.sqrt(periods),
        "win_rate": sum(1 for x in pnl if x > 0) / len(pnl),
        "exposure": exposed / n,
    }


def test_precision_over_one_million_bars_matches_double_reference() -> None:
    trades, equity, initial_cash = _precision_series()
    started = time.perf_counter()
    summary = compute(trades, equity, initial_cash=initial_cash, timeframe="1m")
    elapsed = time.perf_counter() - started
    expected = _reference(trades, equity, initial_cash, 525_960.0)
    for name, value in expected.items():
        assert getattr(summary, name) == pytest.approx(value, rel=PRECISION_TOL), name
    assert summary.n_bars == N_PRECISION
    assert elapsed < 5.0


def test_cagr_is_null_when_annualisation_is_not_finite() -> None:
    # 2 barras 1h con +10 %: exponente 8 766 / 2 = 4 383 → 1,1^4 383 desborda Float32 (deriva RF-7).
    equity = _equity([105.0, 110.0], [0.0, 0.0], step_ms=3_600_000)
    summary = compute(_trades([]), equity, initial_cash=100.0, timeframe="1h")
    assert summary.cagr is None
    assert summary.total_return == pytest.approx(0.1, rel=REL)
