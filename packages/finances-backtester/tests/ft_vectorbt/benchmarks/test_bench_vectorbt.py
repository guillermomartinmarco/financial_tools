"""V7.1: benchmark de desempeno de ``ft_vectorbt`` (RF-12).

Escenario unico sobre un dataset sintetico determinista de ``rows`` velas ``1m`` (por
defecto 1.000.000):

- **B1**: ``VectorbtEngine.run`` (conversion + simulacion + conversion) con las senales de
  ``sma_cross(20, 100)`` **precalculadas por el nucleo** (fuera del cronometro; su calculo
  es del nucleo, RF-12). Mide solo el adaptador del motor.

Los tests marcados ``benchmark`` estan excluidos por defecto (``addopts = -m 'not
benchmark'``); se lanzan con ``python -m pytest -m benchmark -s``. Los tests sin marcador de
este modulo verifican la maquinaria con datos pequenos.

Variables de entorno (solo de benchmark; no son configuracion ``FT_`` de la suite):

- ``BENCH_OUTPUT``: ruta del JSON de resultados (se fusiona por escenario).
- ``BENCH_REPEATS``: repeticiones (por defecto ``thresholds.json``: 5).
- ``BENCH_ROWS``: filas del dataset sintetico (por defecto ``thresholds.json``: 1.000.000).
- ``BENCH_ENFORCE``: ``1``/``true`` hace fallar el test si la mediana supera el umbral de
  ``thresholds.json`` (unica fuente). Provisional hasta su calibracion en CI (ubuntu-24.04).
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from finances_core.backtest.catalog import BUILTIN_DIR, build_registry
from finances_core.backtest.engine_api import CostModel, Signals

from finances_backtester.ft_vectorbt.engine import VectorbtEngine

THRESHOLDS_PATH = Path(__file__).resolve().parent / "thresholds.json"
RESULTS_SCHEMA_VERSION = 1
KIND = "sma_cross"
#: 2024-01-01T00:00:00Z en ms y paso de un minuto.
START_MS = 1_704_067_200_000
STEP_MS = 60_000
INITIAL_CASH = 10_000.0
SIZE_FRACTION = 1.0
COSTS = CostModel(fees_bps=10.0, slippage_bps=0.0)


# ------------------------------------------------------------------ configuracion


def load_thresholds() -> dict[str, Any]:
    """Lee ``thresholds.json`` (unica fuente de umbrales, filas, semilla y repeticiones)."""
    data: dict[str, Any] = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    return data


@dataclass(frozen=True)
class BenchConfig:
    rows: int
    repeats: int
    seed: int
    enforce: bool
    output: Path | None


def bench_config(env: dict[str, str] | None = None) -> BenchConfig:
    """Configuracion del benchmark: ``thresholds.json`` con overrides ``BENCH_*``."""
    values = os.environ if env is None else env
    thresholds = load_thresholds()
    output = values.get("BENCH_OUTPUT")
    return BenchConfig(
        rows=int(values.get("BENCH_ROWS", thresholds["rows"])),
        repeats=int(values.get("BENCH_REPEATS", thresholds["repeats"])),
        seed=int(thresholds["seed"]),
        enforce=values.get("BENCH_ENFORCE", "").strip().lower() in {"1", "true", "yes"},
        output=Path(output) if output else None,
    )


# ------------------------------------------------------------------ datos


def synthetic_candles(rows: int, seed: int) -> pl.DataFrame:
    """Velas ``1m`` canonicas deterministas (random walk sembrado; ``Float32``/``Int64``).

    ``open`` es el ``close`` previo; ``high``/``low`` envuelven ``open``/``close`` con un
    ruido pequeno. Determinista entre plataformas (NumPy ``default_rng``), igual que exige el
    benchmark de regresion.
    """
    rng = np.random.default_rng(seed)
    walk = np.cumsum(rng.standard_normal(rows))
    close = np.abs(walk) + 1.0
    open_ = np.empty(rows, dtype="float64")
    open_[0] = close[0]
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) + np.abs(rng.standard_normal(rows)) * 0.1
    low = np.maximum(np.minimum(open_, close) - np.abs(rng.standard_normal(rows)) * 0.1, 0.01)
    times = START_MS + np.arange(rows, dtype="int64") * STEP_MS
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series(times, dtype=pl.Int64),
            "open": pl.Series(open_, dtype=pl.Float32),
            "high": pl.Series(high, dtype=pl.Float32),
            "low": pl.Series(low, dtype=pl.Float32),
            "close": pl.Series(close, dtype=pl.Float32),
            "volume": pl.Series(np.full(rows, 10.0), dtype=pl.Float32),
        }
    )


def sma_signals(candles: pl.DataFrame) -> Signals:
    """Senales de ``sma_cross`` con sus parametros por defecto (20/100), via el nucleo."""
    strategy = build_registry(builtin_dir=BUILTIN_DIR, local_dir=None).get(KIND)
    params = strategy.Params.model_validate(dict(strategy.default_params))
    return strategy.signals(candles, params)


# ------------------------------------------------------------------ medicion


def measure(fn: Callable[[], object], repeats: int) -> list[float]:
    """Segundos de pared de ``repeats`` ejecuciones de ``fn``."""
    times: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        times.append(time.perf_counter() - started)
    return times


def summarize(
    scenario: str, times_s: list[float], rows: int, threshold_s: float, *, enforced: bool
) -> dict[str, Any]:
    """Resumen de un escenario: mediana, filas/s y comparacion con el umbral."""
    median = statistics.median(times_s)
    return {
        "scenario": scenario,
        "times_s": [round(t, 6) for t in times_s],
        "median_s": round(median, 6),
        "rows_per_s": round(rows / median, 1) if median > 0 else None,
        "threshold_s": threshold_s,
        "within_threshold": median <= threshold_s,
        "enforced": enforced,
    }


def write_results(output: Path, cfg: BenchConfig, summary: dict[str, Any]) -> dict[str, Any]:
    """Fusiona ``summary`` en el JSON de resultados (un escenario por clave)."""
    previous: dict[str, Any] = {}
    if output.is_file():
        previous = json.loads(output.read_text(encoding="utf-8"))
    scenarios: dict[str, Any] = dict(previous.get("scenarios", {}))
    scenarios[summary["scenario"]] = summary
    payload = {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "tool": "ft_vectorbt",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": cfg.rows,
        "timeframe": "1m",
        "seed": cfg.seed,
        "repeats": cfg.repeats,
        "thresholds_status": load_thresholds()["status"],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "polars": pl.__version__,
            "cpu_count": os.cpu_count(),
            "github_runner": os.environ.get("RUNNER_OS"),
        },
        "scenarios": scenarios,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def report(summary: dict[str, Any], cfg: BenchConfig) -> None:
    """Imprime la mediana (visible con ``-s``), escribe el JSON y aplica el umbral."""
    line = (
        f"[{summary['scenario']}] rows={cfg.rows} repeats={cfg.repeats} "
        f"median={summary['median_s']:.4f}s rows/s={summary['rows_per_s']} "
        f"threshold={summary['threshold_s']}s enforced={summary['enforced']}"
    )
    print(line)  # noqa: T201 - salida de benchmark (no modo servicio)
    if cfg.output is not None:
        write_results(cfg.output, cfg, summary)
    if summary["enforced"] and not summary["within_threshold"]:
        pytest.fail(
            f"{summary['scenario']}: mediana {summary['median_s']:.4f}s > umbral "
            f"{summary['threshold_s']}s ({THRESHOLDS_PATH.name})"
        )


def _scenario_threshold(scenario: str) -> float:
    return float(load_thresholds()["scenarios"][scenario]["max_median_s"])


# ------------------------------------------------------------------ maquinaria (sin marcador)


def test_thresholds_file_is_the_single_source() -> None:
    thresholds = load_thresholds()
    assert thresholds["rows"] == 1_000_000
    assert thresholds["repeats"] == 5
    assert set(thresholds["scenarios"]) == {"B1"}
    assert _scenario_threshold("B1") == thresholds["scenarios"]["B1"]["max_median_s"]


def test_bench_config_reads_env_overrides(tmp_path: Path) -> None:
    default = bench_config({})
    assert (default.rows, default.repeats, default.enforce, default.output) == (
        1_000_000,
        5,
        False,
        None,
    )
    out = tmp_path / "r.json"
    cfg = bench_config(
        {"BENCH_ROWS": "1000", "BENCH_REPEATS": "2", "BENCH_ENFORCE": "1", "BENCH_OUTPUT": str(out)}
    )
    assert (cfg.rows, cfg.repeats, cfg.enforce, cfg.output) == (1000, 2, True, out)


def test_synthetic_candles_deterministic_and_canonical() -> None:
    first = synthetic_candles(5_000, seed=1)
    assert first.equals(synthetic_candles(5_000, seed=1))
    assert not first.equals(synthetic_candles(5_000, seed=2))
    assert first.schema["open_time_ms"] == pl.Int64
    assert first.schema["close"] == pl.Float32
    times = first["open_time_ms"]
    assert times.is_sorted()
    assert times.diff().drop_nulls().unique().to_list() == [STEP_MS]
    assert (first["high"] >= first["low"]).all()
    assert (first["high"] >= first["close"]).all()
    assert (first["low"] <= first["close"]).all()


def test_summary_and_results_json(tmp_path: Path) -> None:
    cfg = bench_config({"BENCH_ROWS": "100", "BENCH_REPEATS": "3"})
    b1 = summarize("B1", [0.3, 0.1, 0.2], 100, 2.0, enforced=False)
    assert b1["median_s"] == 0.2
    assert b1["rows_per_s"] == 500.0
    assert b1["within_threshold"] is True
    out = tmp_path / "bench" / "results.json"
    payload = write_results(out, cfg, b1)
    assert set(payload["scenarios"]) == {"B1"}
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_report_fails_only_when_enforced(tmp_path: Path) -> None:
    cfg = bench_config({"BENCH_ROWS": "100", "BENCH_OUTPUT": str(tmp_path / "r.json")})
    report(summarize("B1", [3.0], 100, 2.0, enforced=False), cfg)
    with pytest.raises(pytest.fail.Exception, match="umbral"):
        report(summarize("B1", [3.0], 100, 2.0, enforced=True), cfg)


def test_small_engine_run_produces_trades() -> None:
    """B1 en miniatura: el motor corre con senales del nucleo y deja operaciones."""
    candles = synthetic_candles(2_000, seed=7)
    output = VectorbtEngine().run(candles, sma_signals(candles), COSTS, SIZE_FRACTION, INITIAL_CASH)
    assert output.trades.height >= 1
    assert output.equity.height == candles.height


# ------------------------------------------------------------------ escenario


@pytest.mark.benchmark
def test_b1_engine_run_in_memory() -> None:
    cfg = bench_config()
    candles = synthetic_candles(cfg.rows, cfg.seed)
    signals = sma_signals(candles)  # precalculado por el nucleo, fuera del cronometro (RF-12)
    engine = VectorbtEngine()
    times = measure(
        lambda: engine.run(candles, signals, COSTS, SIZE_FRACTION, INITIAL_CASH), cfg.repeats
    )
    report(summarize("B1", times, cfg.rows, _scenario_threshold("B1"), enforced=cfg.enforce), cfg)
