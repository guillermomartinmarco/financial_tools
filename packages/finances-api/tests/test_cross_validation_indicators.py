"""Validación cruzada ``ft_indicators`` ↔ ``ft_ta_lib`` (``ft_ta_lib`` RF-18, plan §3.5, T6.2).

Solo HTTP sobre la app compuesta (``finances_api.create_app()``): ningún módulo de este test
importa ``finances_analytics`` (Art. 4; lo verifica :func:`test_module_does_not_import_tools`).

1. Dataset sintético Hive (paseo aleatorio de 5.000 velas ``1h``) en un ``FT_PROJECT_ROOT``
   temporal.
2. ``GET /indicators/catalog`` → para cada indicador con ``talib_equivalent``, la spec de
   ``/ta-lib`` sale de su mapeo legible por máquina (parámetro TA-Lib → parámetro propio o
   literal; salida propia → ``FUNCIÓN:salida``) con los defaults publicados.
3. ``POST /indicators/compute`` y ``POST /ta-lib/compute`` con *polling* hasta ``COMPLETED``;
   ambas tablas se exportan en Parquet.
4. Cada columna ``Float32`` de ``ft_indicators`` se compara con su equivalente de
   ``ft_ta_lib`` con la tabla de tolerancias publicada (``f32_tolerance``:
   ``|x − ref| ≤ atol + atol_scaled·escala + rtol·|ref|``; escala = media de ``|close|`` o
   ``Σ|volume|`` en ``obv``, plan de ``ft_indicators`` §3.9) más unos pocos ULP de ``Float32``
   relativos a la escala de la columna (``ft_ta_lib`` persiste TA-Lib redondeado a
   ``Float32``; nunca se exige igualdad bit a bit entre máquinas).

Convenciones documentadas que se respetan: comparación desde ``stable_from_ms`` de
``ft_indicators`` y desde ``compare_from`` (MACD desde ``stable_from`` = fila 267 con los
defaults, Stochastic desde la fila 17); filas declaradas en ``excluded_rows`` (ROC con base
0: TA-Lib da 0, ``ft_indicators`` nulo); ADX con la semilla de TA-Lib; ``vwap`` no tiene
equivalente. El test falla si aparece un ``talib_equivalent`` sin comparar.
"""

from __future__ import annotations

import ast
import io
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.market_data import OHLCV_SCHEMA

from finances_api.app import create_app

ROWS = 5_000
START_MS = 1_704_067_200_000
HOUR_MS = 3_600_000
DATASET = {"exchange": "binance", "market_type": "spot", "symbol": "XVALUSDT", "timeframe": "1h"}
DS_PATH = "binance/spot/XVALUSDT/1h"
#: Indicadores cuya escala de tolerancia es ``Σ|volume|`` (plan de ``ft_indicators`` §3.9).
VOLUME_SCALED = frozenset({"obv"})
#: Indicadores sin equivalente TA-Lib (diferencia documentada en ``ft_indicators`` RF-2).
WITHOUT_EQUIVALENT = frozenset({"vwap"})
#: ULP de ``Float32`` (2⁻²³) y holgura en ULP relativa a la escala de la columna.
F32_EPS = float(np.finfo(np.float32).eps)
ULP_SLACK = 4


@pytest.fixture(autouse=True)
def _isolate_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FT_PROJECT_ROOT", str(tmp_path))


def _write_random_walk(rows: int = ROWS, seed: int = 20_241_025) -> None:
    """Paseo aleatorio OHLCV determinista en ``binance/spot/XVALUSDT/1h`` (esquema canónico)."""
    rng = random.Random(seed)
    closes: list[float] = []
    price = 100.0
    for _ in range(rows):
        price = max(0.01, price * (1.0 + rng.gauss(0.0, 0.01)))
        closes.append(price)
    opens = [100.0, *closes[:-1]]
    highs = [max(o, c) * (1 + abs(rng.gauss(0, 0.004))) for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) * (1 - abs(rng.gauss(0, 0.004))) for o, c in zip(opens, closes, strict=True)]
    volumes = [abs(rng.gauss(1000.0, 250.0)) for _ in range(rows)]
    frame = pl.DataFrame(
        {
            "symbol": DATASET["symbol"],
            "timeframe": DATASET["timeframe"],
            "market_type": DATASET["market_type"],
            "open_time_ms": [START_MS + i * HOUR_MS for i in range(rows)],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "quote_volume": [v * c for v, c in zip(volumes, closes, strict=True)],
            "trades_count": [10] * rows,
            "taker_buy_base_volume": [v / 2 for v in volumes],
            "taker_buy_quote_volume": [v * c / 2 for v, c in zip(volumes, closes, strict=True)],
        }
    )
    dt = pl.from_epoch(pl.col("open_time_ms"), time_unit="ms")
    frame = frame.select([pl.col(c).cast(t) for c, t in OHLCV_SCHEMA.items()]).with_columns(
        dt.dt.year().alias("year"), dt.dt.month().alias("month")
    )
    write_parquet_partitioned(
        frame, FinancesSettings().PARQUET_DIR, exchange="binance", schema=dict(OHLCV_SCHEMA)
    )


# ------------------------------------------------------------------ HTTP


def _compute_and_wait(client: TestClient, prefix: str, body: dict[str, Any]) -> str:
    response = client.post(f"{prefix}/compute", json=body)
    assert response.status_code == 202, response.text
    poll_url = response.json()["poll_url"]
    assert f"{prefix}/status/" in poll_url
    status: dict[str, Any] = {}
    for _ in range(200):
        status = client.get(poll_url).json()
        if status["status"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.05)
    assert status["status"] == "COMPLETED", status
    assert status["errors"] == [], status["errors"]
    task_id: str = response.json()["task_id"]
    return task_id


def _export(client: TestClient, prefix: str, *, include_ohlcv: bool) -> pl.DataFrame:
    response = client.post(
        f"{prefix}/datasets/{DS_PATH}/export",
        json={"format": "parquet", "include_ohlcv": include_ohlcv},
    )
    assert response.status_code == 200, response.text
    return pl.read_parquet(io.BytesIO(response.content))


def _literal(expr: str) -> int | float:
    try:
        return int(expr)
    except ValueError:
        return float(expr)


@dataclass(frozen=True)
class Pair:
    """Una salida de ``ft_indicators`` y su equivalente de ``ft_ta_lib``."""

    indicator: str
    key: str  # clave publicada (sufijo o nombre del indicador)
    ours: str  # columna de ft_indicators
    theirs: str  # columna de ft_ta_lib
    tolerance: dict[str, float]
    compare_from: str | int
    excluded_rows: str | None


def _talib_specs(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Spec de ``/ta-lib`` por función TA-Lib, desde ``talib_equivalent`` y los defaults."""
    specs: dict[str, dict[str, Any]] = {}
    for entry in entries:
        equivalent = entry["talib_equivalent"]
        if equivalent is None:
            continue
        own = {name: meta["default"] for name, meta in entry["params"].items()}
        params = {
            tp: own[expr] if expr in own else _literal(expr)
            for tp, expr in equivalent["params"].items()
        }
        inputs = {"price": own["source"]} if own.get("source", "close") != "close" else {}
        for target in equivalent["outputs"].values():
            function = target.split(":")[0]
            spec = {"function": function, "params": params, "inputs": inputs}
            assert specs.setdefault(function, spec) == spec, function
    return specs


def _ta_lib_columns(
    client: TestClient, plan: dict[str, Any], functions: list[str]
) -> dict[str, dict[str, str]]:
    """``FUNCIÓN`` → salida TA-Lib → columna persistida por ``ft_ta_lib``."""
    by_function = {item["spec"]["function"]: item for item in plan["items"]}
    columns: dict[str, dict[str, str]] = {}
    for function in functions:
        item = by_function[function]
        entry = client.get("/ta-lib/catalog", params={"function": function}).json()
        outputs = [o["name"] for o in entry["functions"][0]["outputs"]]
        sid = item["series_id"]
        mapping = {o: sid if len(outputs) == 1 else f"{sid}_{o.lower()}" for o in outputs}
        assert set(mapping.values()) == set(item["columns"]), (function, item["columns"])
        columns[function] = mapping
    return columns


def _pairs(
    entries: list[dict[str, Any]],
    indicators_plan: dict[str, Any],
    ta_columns: dict[str, dict[str, str]],
) -> list[Pair]:
    ids = {item["spec"]["name"]: item for item in indicators_plan["items"]}
    pairs: list[Pair] = []
    for entry in entries:
        equivalent = entry["talib_equivalent"]
        if equivalent is None:
            continue
        name = entry["name"]
        item = ids[name]
        for key, target in equivalent["outputs"].items():
            ours = item["indicator_id"] if key == name else f"{item['indicator_id']}_{key}"
            assert ours in item["columns"], (name, key, item["columns"])
            function, output = target.split(":")
            pairs.append(
                Pair(
                    indicator=name,
                    key=key,
                    ours=ours,
                    theirs=ta_columns[function][output],
                    tolerance=entry["f32_tolerance"][key],
                    compare_from=equivalent["compare_from"],
                    excluded_rows=equivalent["excluded_rows"],
                )
            )
    return pairs


# ------------------------------------------------------------------ comparación


def _first_non_null(values: np.ndarray[Any, Any]) -> int:
    valid = ~np.isnan(values)
    return int(np.argmax(valid)) if valid.any() else len(values)


def _compare(
    pair: Pair,
    frame: pl.DataFrame,
    times: list[int],
    stable_from_ms: int | None,
    scale: float,
) -> dict[str, float]:
    """Compara una columna y devuelve ``{max_abs, max_ratio, start, rows}``.

    ``max_ratio`` = máx. de ``|x − ref| / admitido`` (≤ 1 dentro de tolerancia).
    """
    ours = frame[pair.ours].cast(pl.Float64).fill_null(np.nan).to_numpy()
    ref = frame[pair.theirs].cast(pl.Float64).fill_null(np.nan).to_numpy()
    start = times.index(stable_from_ms) if stable_from_ms is not None else 0
    if pair.compare_from == "first_non_null":
        first_ours, first_ref = _first_non_null(ours), _first_non_null(ref)
        assert first_ours == first_ref, f"{pair.ours}: calentamiento {first_ours} != {first_ref}"
        start = max(start, first_ours)
    elif isinstance(pair.compare_from, int):
        start = max(start, pair.compare_from)
    # "stable_from": ya cubierto por stable_from_ms de ft_indicators.
    rows = np.arange(len(ours)) >= start
    ours_null, ref_null = np.isnan(ours), np.isnan(ref)
    excluded = np.zeros(len(ours), dtype=bool)
    if pair.excluded_rows is not None:  # p. ej. ROC base 0: ft_indicators nulo, TA-Lib 0
        excluded = ours_null & (ref == 0)
    mismatched = rows & ~excluded & (ours_null != ref_null)
    assert not mismatched.any(), (
        f"{pair.ours}: nulos distintos en filas {np.flatnonzero(mismatched)[:5]}"
    )
    mask = rows & ~excluded & ~ours_null & ~ref_null
    assert mask.sum() > 0, f"{pair.ours}: nada que comparar desde la fila {start}"
    a, b = ours[mask], ref[mask]
    tol = pair.tolerance
    column_scale = float(np.abs(b).max())
    allowed = (
        tol["atol"]
        + tol["atol_scaled"] * scale
        + tol["rtol"] * np.abs(b)
        + ULP_SLACK * F32_EPS * column_scale
    )
    diff = np.abs(a - b)
    worst = int(np.argmax(diff / allowed))
    assert (diff <= allowed).all(), (
        f"{pair.indicator}.{pair.key} ({pair.ours} vs {pair.theirs}): "
        f"|Δ|={diff[worst]:.3g} > {allowed[worst]:.3g} en ref={b[worst]:.6g}"
    )
    return {
        "max_abs": float(diff.max()),
        "max_ratio": float((diff / allowed).max()),
        "start": float(start),
        "rows": float(mask.sum()),
    }


def cross_validate(client: TestClient) -> dict[str, dict[str, float]]:
    """Ejecuta la validación cruzada completa; devuelve las métricas por columna comparada."""
    entries: list[dict[str, Any]] = client.get("/indicators/catalog").json()
    names = [e["name"] for e in entries]
    assert {e["name"] for e in entries if e["talib_equivalent"] is None} == WITHOUT_EQUIVALENT

    specs = _talib_specs(entries)
    indicators_task = _compute_and_wait(
        client,
        "/indicators",
        {"datasets": [DATASET], "indicators": [{"name": n} for n in names]},
    )
    ta_task = _compute_and_wait(
        client, "/ta-lib", {"datasets": [DATASET], "functions": list(specs.values())}
    )
    indicators_plan = client.get(f"/indicators/plan/{indicators_task}").json()
    ta_plan = client.get(f"/ta-lib/plan/{ta_task}").json()
    pairs = _pairs(entries, indicators_plan, _ta_lib_columns(client, ta_plan, list(specs)))

    expected = {
        (e["name"], key)
        for e in entries
        if e["talib_equivalent"] is not None
        for key in e["talib_equivalent"]["outputs"]
    }
    assert {(p.indicator, p.key) for p in pairs} == expected

    ours = _export(client, "/indicators", include_ohlcv=True)
    theirs = _export(client, "/ta-lib", include_ohlcv=False).drop(
        "exchange", "market_type", "symbol", "timeframe"
    )
    frame = ours.join(theirs, on="open_time_ms", how="inner")
    assert frame.height == ours.height == theirs.height == ROWS
    times = frame["open_time_ms"].to_list()
    manifest = client.get(f"/indicators/datasets/{DS_PATH}").json()["entries"]
    stable = {
        column: value
        for entry in manifest.values()
        for column, value in entry["stability"]["by_column"].items()
    }
    price_scale = float(frame["close"].abs().mean())
    volume_scale = float(frame["volume"].abs().sum())

    report: dict[str, dict[str, float]] = {}
    for pair in pairs:
        scale = volume_scale if pair.indicator in VOLUME_SCALED else price_scale
        report[pair.ours] = _compare(pair, frame, times, stable[pair.ours], scale)
    compared = {(p.indicator, p.key) for p in pairs if p.ours in report}
    assert compared == expected, f"talib_equivalent sin comparar: {sorted(expected - compared)}"
    return report


def test_ft_indicators_and_ft_ta_lib_agree_within_published_tolerance() -> None:
    _write_random_walk()
    report = cross_validate(TestClient(create_app()))
    # 11 indicadores con equivalente: 18 columnas (macd 3, bb 3, stoch 2, adx 3, 7 de una).
    assert len(report) == 18
    macd_signal = report["macd_12_26_9_signal"]
    assert macd_signal["start"] == 267  # compare_from = stable_from
    assert report["stoch_14_3_3_k"]["start"] >= 17  # compare_from = k + smooth_k + d - 3
    assert all(r["max_ratio"] <= 1.0 for r in report.values())


def test_module_does_not_import_tools() -> None:
    """Art. 4: el test usa solo HTTP; no importa ``finances_analytics`` ni sus herramientas."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    forbidden = {m for m in modules if m.split(".")[0] in {"finances_analytics", "talib"}}
    assert forbidden == set()
