"""T6.1: benchmarks de desempeño de ``ft_indicators`` (RF-14).

Escenarios sobre un dataset sintético determinista de ``rows`` velas ``1m`` (por defecto
1.000.000; generador compartido con ``ft_ta_lib`` en ``tests/datasets.py``) y los 12
indicadores del catálogo con parámetros por defecto (19 columnas):

- **B1**: motor en memoria (``engine.compute``, sin IO).
- **B2**: tarea extremo a extremo (planificar + leer Parquet + calcular + reescribir
  particiones y manifiesto) con ``service.prepare_compute``/``create_task``/``run_compute``.

Los tests marcados ``benchmark`` están excluidos de la ejecución por defecto
(``addopts = -m 'not benchmark'``); se lanzan con ``python -m pytest -m benchmark -s``.
Los tests sin marcador de este módulo verifican la maquinaria con datos pequeños.

Variables de entorno (solo de benchmark; no son configuración ``FT_`` de la suite):

- ``BENCH_OUTPUT``: ruta del JSON de resultados (se fusiona por escenario). Sin ella no se
  escribe archivo.
- ``BENCH_REPEATS``: repeticiones por escenario (por defecto ``thresholds.json``: 5).
- ``BENCH_ROWS``: filas del dataset sintético (por defecto ``thresholds.json``: 1.000.000).
- ``BENCH_ENFORCE``: ``1``/``true`` hace fallar el test si la mediana supera el umbral de
  ``thresholds.json`` (única fuente de umbrales). Desactivado hasta la calibración (T6.3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.logging import configure_logging
from finances_core.schemas.indicators import IndicatorComputeRequest, IndicatorSpec
from finances_core.schemas.market_data import OHLCV_SCHEMA, TF_MS_MAP

from finances_analytics.ft_indicators import engine, service
from finances_analytics.ft_indicators.catalog import CATALOG, ResolvedIndicator, resolve

from ...datasets import BENCH_DATASET, JAN_2024_MS, synthetic_ohlcv_1m, write_bench_source

THRESHOLDS_PATH = Path(__file__).resolve().parent / "thresholds.json"
#: 2024-01-01T00:00:00Z en ms.
START_MS = JAN_2024_MS
RESULTS_SCHEMA_VERSION = 1
DATASET = BENCH_DATASET
INPUT_COLUMNS = ["open_time_ms", "open", "high", "low", "close", "volume"]


# ------------------------------------------------------------------ configuración


def load_thresholds() -> dict[str, Any]:
    """Lee ``thresholds.json`` (única fuente de umbrales, filas, semilla y repeticiones)."""
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
    """Configuración del benchmark: ``thresholds.json`` con overrides ``BENCH_*``."""
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


def write_source(frame: pl.DataFrame, settings: FinancesSettings) -> None:
    """Escribe el dataset sintético bajo ``FT_PARQUET_DIR`` con el escritor Hive del núcleo."""
    write_bench_source(frame, settings.PARQUET_DIR, DATASET)


def default_indicators() -> list[ResolvedIndicator]:
    """Los 12 indicadores del catálogo con parámetros por defecto."""
    return [resolve(IndicatorSpec(name=name)) for name in CATALOG]


def default_request() -> IndicatorComputeRequest:
    return IndicatorComputeRequest(
        datasets=[DATASET], indicators=[IndicatorSpec(name=name) for name in CATALOG]
    )


# ------------------------------------------------------------------ medición


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
    """Resumen de un escenario: mediana, filas/s y comparación con el umbral."""
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
        "tool": "ft_indicators",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": cfg.rows,
        "timeframe": "1m",
        "seed": cfg.seed,
        "repeats": cfg.repeats,
        "indicators": len(CATALOG),
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
        f"median={summary['median_s']:.3f}s rows/s={summary['rows_per_s']} "
        f"threshold={summary['threshold_s']}s enforced={summary['enforced']}"
    )
    print(line)  # noqa: T201 - salida de benchmark (no modo servicio)
    if cfg.output is not None:
        write_results(cfg.output, cfg, summary)
    if summary["enforced"] and not summary["within_threshold"]:
        pytest.fail(
            f"{summary['scenario']}: mediana {summary['median_s']:.3f}s > umbral "
            f"{summary['threshold_s']}s ({THRESHOLDS_PATH.name})"
        )


def _scenario_threshold(scenario: str) -> float:
    return float(load_thresholds()["scenarios"][scenario]["max_median_s"])


# ------------------------------------------------------------------ maquinaria (sin marcador)


def test_thresholds_file_is_the_single_source() -> None:
    thresholds = load_thresholds()
    assert thresholds["rows"] == 1_000_000
    assert thresholds["repeats"] == 5
    assert set(thresholds["scenarios"]) == {"B1", "B2"}
    for scenario, spec in thresholds["scenarios"].items():
        assert _scenario_threshold(scenario) == spec["max_median_s"]
    calibration = thresholds["calibration"]
    if calibration is not None:  # T6.3: 1,5 x mediana de CI, sin superar el provisional
        for scenario, provisional in calibration["provisional_s"].items():
            assert _scenario_threshold(scenario) <= provisional


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


def test_synthetic_dataset_is_deterministic_and_canonical() -> None:
    first = synthetic_ohlcv_1m(5_000, seed=1)
    second = synthetic_ohlcv_1m(5_000, seed=1)
    assert first.equals(second)
    assert not first.equals(synthetic_ohlcv_1m(5_000, seed=2))
    assert first.select(list(OHLCV_SCHEMA)).schema == pl.Schema(OHLCV_SCHEMA)
    times = first["open_time_ms"]
    assert times.is_sorted()
    assert times.diff().drop_nulls().unique().to_list() == [TF_MS_MAP["1m"]]
    assert (first["high"] >= first["low"]).all()


def test_default_catalogue_has_12_indicators_and_19_columns() -> None:
    indicators = default_indicators()
    assert len(indicators) == 12
    assert sum(len(r.columns) for r in indicators) == 19


def test_summary_and_results_json(tmp_path: Path) -> None:
    cfg = bench_config({"BENCH_ROWS": "100", "BENCH_REPEATS": "3"})
    b1 = summarize("B1", [0.3, 0.1, 0.2], 100, 2.0, enforced=False)
    assert b1["median_s"] == 0.2
    assert b1["rows_per_s"] == 500.0
    assert b1["within_threshold"] is True
    out = tmp_path / "bench" / "results.json"
    write_results(out, cfg, b1)
    payload = write_results(out, cfg, summarize("B2", [9.0], 100, 8.0, enforced=False))
    assert set(payload["scenarios"]) == {"B1", "B2"}
    assert json.loads(out.read_text(encoding="utf-8")) == payload
    assert payload["scenarios"]["B2"]["within_threshold"] is False


def test_report_fails_only_when_enforced(tmp_path: Path) -> None:
    cfg = bench_config({"BENCH_ROWS": "100", "BENCH_OUTPUT": str(tmp_path / "r.json")})
    report(summarize("B1", [3.0], 100, 2.0, enforced=False), cfg)
    with pytest.raises(pytest.fail.Exception, match="umbral"):
        report(summarize("B1", [3.0], 100, 2.0, enforced=True), cfg)


def test_small_end_to_end_run_writes_all_series(settings: FinancesSettings) -> None:
    """B2 en miniatura: la tarea escribe las 12 series del dataset sintético."""
    write_source(synthetic_ohlcv_1m(500, seed=3), settings)
    _run_end_to_end(settings)
    manifest = service.get_manifest(DATASET, settings)
    assert manifest is not None
    assert len(manifest.entries) == 12


# ------------------------------------------------------------------ escenarios


def _run_end_to_end(settings: FinancesSettings) -> None:
    prepared = service.prepare_compute(default_request(), settings)
    service.create_task(prepared, settings)
    result = asyncio.run(service.run_compute(prepared, settings))
    assert result.status == "COMPLETED", result.errors


@pytest.mark.benchmark
def test_b1_engine_in_memory() -> None:
    cfg = bench_config()
    indicators = default_indicators()
    frame = synthetic_ohlcv_1m(cfg.rows, seed=cfg.seed).select(INPUT_COLUMNS)
    times = measure(lambda: engine.compute(frame, indicators), cfg.repeats)
    report(summarize("B1", times, cfg.rows, _scenario_threshold("B1"), enforced=cfg.enforce), cfg)


@pytest.mark.benchmark
def test_b2_end_to_end_task(settings: FinancesSettings) -> None:
    cfg = bench_config()
    configure_logging("cli", logging.WARNING, component="ft_indicators")
    write_source(synthetic_ohlcv_1m(cfg.rows, seed=cfg.seed), settings)
    times = measure(lambda: _run_end_to_end(settings), cfg.repeats)
    report(summarize("B2", times, cfg.rows, _scenario_threshold("B2"), enforced=cfg.enforce), cfg)
