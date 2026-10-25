"""Paquete de *fixtures* del oráculo (RF-17, plan §3.10).

Herramienta de desarrollo (no es funcionalidad de usuario; Art. 1 no aplica): genera, a
partir de ``ft_indicators`` (no de TA-Lib), un paquete versionado y legible sin Python
para la futura batería de tests de kernels ``f32`` (GPU/WGSL/CUDA/C++)::

    {out_dir}/{version}/
    ├── manifest.json                       # versión, EPSILON, tolerancias, convenciones,
    │                                       # commit generador, stable_from_index, sha256
    ├── inputs/{case}.parquet|.csv          # open_time_ms Int64 + OHLCV Float32 (canónico)
    └── expected/{case}/{indicator_id}.parquet|.csv   # open_time_ms + salidas del oráculo

CSV: separador ``,``, cabecera, fin de línea LF, nulo = celda vacía, ``NaN``/``Infinity``
literales y reales con ``%.17g`` (``float()``/``strtod`` recuperan exactamente el valor).
Los parámetros de los indicadores son los defaults del catálogo.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable
from pathlib import Path

import polars as pl
from finances_core.schemas.indicators import IndicatorSpec

from . import engine
from .catalog import CATALOG, ENGINE_VERSION, ResolvedIndicator, resolve
from .convergence import EPSILON
from .precision import TOLERANCES

#: Versión del formato del paquete (cambia si cambia la estructura, no las fórmulas).
SCHEMA_VERSION = 1
SEED = 20_261_025
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
STEP_MS = 3_600_000  # velas 1h
PRICE_COLUMNS = ("open", "high", "low", "close")
INPUT_COLUMNS = ("open_time_ms", *PRICE_COLUMNS, "volume")

Columns = dict[str, list[float | None]]


def _random_walk(n: int, seed: int, scale: float = 1.0) -> Columns:
    rng = random.Random(seed)
    closes: list[float] = []
    price = 100.0
    for _ in range(n):
        price = max(0.01, price * (1.0 + rng.gauss(0.0, 0.01)))
        closes.append(price)
    opens = [100.0, *closes[:-1]]
    highs = [
        max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.004))) for o, c in zip(opens, closes, strict=True)
    ]
    lows = [
        min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.004))) for o, c in zip(opens, closes, strict=True)
    ]
    volumes: list[float | None] = [abs(rng.gauss(1000.0, 250.0)) for _ in range(n)]
    cols: Columns = {
        "open": [v * scale for v in opens],
        "high": [v * scale for v in highs],
        "low": [v * scale for v in lows],
        "close": [v * scale for v in closes],
    }
    cols["volume"] = volumes
    return cols


def _flat(n: int) -> Columns:
    cols: Columns = {c: [100.0] * n for c in PRICE_COLUMNS}
    cols["volume"] = [1000.0] * n
    return cols


def _nulls_nan() -> Columns:
    cols = _random_walk(600, SEED + 2)
    for i in range(0, 600, 37):
        cols["close"][i] = None
    for i in range(5, 600, 53):
        cols["high"][i] = math.nan
    for i in range(11, 600, 101):
        cols["volume"][i] = None
    for i in range(200, 205):
        for c in (*PRICE_COLUMNS, "volume"):
            cols[c][i] = None
    return cols


def _hh_eq_ll() -> Columns:
    cols = _random_walk(400, SEED + 5)
    level = cols["close"][149]
    for i in range(150, 220):
        for c in PRICE_COLUMNS:
            cols[c][i] = level
    return cols


#: Casos del paquete (plan §3.10) con su descripción.
CASES: dict[str, tuple[str, Callable[[], Columns]]] = {
    "random_walk": (
        "Paseo aleatorio de 10.000 velas (semilla fija)",
        lambda: _random_walk(10_000, SEED),
    ),
    "flat": ("Serie plana O=H=L=C=100 (RSI/ADX/estocástico = 0)", lambda: _flat(300)),
    "nulls_nan": ("Paseo aleatorio con nulos y NaN intercalados", _nulls_nan),
    "short": (
        "Serie más corta que el calentamiento (10 velas)",
        lambda: _random_walk(10, SEED + 3),
    ),
    "extreme_low": ("Precios extremos ~1e-6", lambda: _random_walk(1_000, SEED + 4, 1e-6)),
    "extreme_high": ("Precios extremos ~1e8", lambda: _random_walk(1_000, SEED + 6, 1e6)),
    "hh_eq_ll": ("Tramo plano H=L=C de 70 velas (HH=LL)", _hh_eq_ll),
}


def _to_frame(cols: Columns) -> pl.DataFrame:
    n = len(cols["close"])
    schema: dict[str, type[pl.DataType]] = dict.fromkeys(INPUT_COLUMNS[1:], pl.Float32)
    data: dict[str, list[float | None] | list[int]] = {
        "open_time_ms": [START_MS + i * STEP_MS for i in range(n)]
    }
    data.update(cols)
    return pl.DataFrame(data, schema={"open_time_ms": pl.Int64, **schema})


def oracle_cases() -> dict[str, pl.DataFrame]:
    """Entradas canónicas (``open_time_ms`` ``Int64`` + OHLCV ``Float32``) de cada caso."""
    return {name: _to_frame(build()) for name, (_desc, build) in CASES.items()}


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return format(value, ".17g")
    return str(value)


def _write_csv(frame: pl.DataFrame, path: Path) -> None:
    lines = [",".join(frame.columns)]
    lines.extend(",".join(_cell(v) for v in row) for row in frame.iter_rows())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _write_pair(frame: pl.DataFrame, base: Path) -> tuple[Path, Path]:
    base.parent.mkdir(parents=True, exist_ok=True)
    parquet, csv_path = base.with_suffix(".parquet"), base.with_suffix(".csv")
    frame.write_parquet(parquet, compression="zstd", statistics=True)
    _write_csv(frame, csv_path)
    return parquet, csv_path


def _defaults() -> list[ResolvedIndicator]:
    return [resolve(IndicatorSpec.model_validate({"name": name})) for name in CATALOG]


def _stable_rows(
    result: engine.EngineResult, resolved: ResolvedIndicator, times: list[int]
) -> dict[str, int | None]:
    by_column = result.outputs[resolved.indicator_id].stability.by_column
    index = {t: i for i, t in enumerate(times)}
    return {c: (index[ms] if ms is not None else None) for c, ms in by_column.items()}


def export_oracle_pack(out_dir: Path, version: str = "v1", *, commit: str = "unknown") -> Path:
    """Genera el paquete ``{out_dir}/{version}`` y devuelve su ruta.

    Los archivos se reescriben por completo; el contenido es determinista salvo
    ``generator.commit`` y los hash de los Parquet (metadatos del escritor).
    """
    root = out_dir / version
    root.mkdir(parents=True, exist_ok=True)
    indicators = _defaults()
    files: list[Path] = []
    cases_meta: dict[str, object] = {}
    for name, frame in oracle_cases().items():
        files.extend(_write_pair(frame, root / "inputs" / name))
        oracle = engine.compute_float64(frame, indicators)
        persisted = engine.compute(frame, indicators)
        times = frame["open_time_ms"].to_list()
        expected: dict[str, object] = {}
        for resolved in indicators:
            columns = list(resolved.columns.values())
            base = root / "expected" / name / resolved.indicator_id
            files.extend(_write_pair(oracle.select("open_time_ms", *columns), base))
            output = persisted.outputs[resolved.indicator_id]
            expected[resolved.indicator_id] = {
                "parquet": base.with_suffix(".parquet").relative_to(root).as_posix(),
                "csv": base.with_suffix(".csv").relative_to(root).as_posix(),
                "columns": columns,
                "stable_from_index": _stable_rows(persisted, resolved, times),
                "stability_reason": output.stability.reason,
                "warnings": list(output.warnings),
            }
        cases_meta[name] = {
            "description": CASES[name][0],
            "rows": frame.height,
            "inputs": {
                "parquet": f"inputs/{name}.parquet",
                "csv": f"inputs/{name}.csv",
            },
            "expected": expected,
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pack_version": version,
        "engine_versions": {"ft_indicators": ENGINE_VERSION},
        "epsilon": EPSILON,
        "generator": {
            "tool": "ft_indicators",
            "commit": commit,
            "script": "packages/finances-analytics/scripts/export_oracle_fixtures.py",
            "seed": SEED,
        },
        "formats": {
            "csv": "UTF-8, ',' , LF, cabecera; nulo = vacío; NaN/Infinity literales; reales %.17g",
            "parquet": "open_time_ms Int64; entradas Float32 (canónico); salidas Float64",
        },
        "comparison": (
            "Solo filas >= stable_from_index (o primera no nula si es null y no hay semilla); "
            "|x_f32 - x_oraculo| <= atol + atol_scaled*escala + rtol*|x_oraculo|; escala = media "
            "de |close| (|source|) o sum|volume| en obv; nulos y divisiones por cero exactos"
        ),
        "indicators": {
            r.indicator_id: {
                "name": r.definition.name,
                "params": r.params,
                "columns": list(r.columns.values()),
                "warmup": r.warmups(),
            }
            for r in indicators
        },
        "conventions": {name: d.convention for name, d in CATALOG.items()},
        "tolerances": {
            name: {key: tol.as_dict() for key, tol in table.items()}
            for name, table in TOLERANCES.items()
        },
        "cases": cases_meta,
        "files": {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)
        },
    }
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
    (root / "manifest.json").write_text(text + "\n", encoding="utf-8", newline="\n")
    return root


__all__ = ["CASES", "SCHEMA_VERSION", "SEED", "export_oracle_pack", "oracle_cases"]
