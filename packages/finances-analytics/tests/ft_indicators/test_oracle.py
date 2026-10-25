"""T3.3: paquete de *fixtures* del oráculo ``fixtures/oracle/v1`` (RF-17, plan §3.10)."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from finances_analytics.ft_indicators.catalog import CATALOG, ENGINE_VERSION
from finances_analytics.ft_indicators.convergence import EPSILON
from finances_analytics.ft_indicators.oracle import (
    CASES,
    SCHEMA_VERSION,
    export_oracle_pack,
    oracle_cases,
)

PACK = Path(__file__).resolve().parent / "fixtures" / "oracle" / "v1"
INPUT_COLUMNS = ["open_time_ms", "open", "high", "low", "close", "volume"]


def _manifest(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("oracle")
    return export_oracle_pack(out, "v1", commit="regenerated-in-test")


def test_cases_cover_plan_3_10() -> None:
    assert set(CASES) == {
        "random_walk",
        "flat",
        "nulls_nan",
        "short",
        "extreme_low",
        "extreme_high",
        "hh_eq_ll",
    }
    cases = oracle_cases()
    assert cases["random_walk"].height == 10_000
    assert cases["short"].height < 14
    nulls = cases["nulls_nan"]
    assert nulls["close"].null_count() > 0
    assert nulls["high"].is_nan().sum() > 0
    for frame in cases.values():
        assert frame.columns == INPUT_COLUMNS
        assert frame.schema["open_time_ms"] == pl.Int64
        assert all(frame.schema[c] == pl.Float32 for c in INPUT_COLUMNS[1:])


def test_versioned_manifest_contents() -> None:
    manifest = _manifest(PACK)
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["pack_version"] == "v1"
    assert manifest["engine_versions"] == {"ft_indicators": ENGINE_VERSION}
    assert manifest["epsilon"] == EPSILON
    assert manifest["generator"]["commit"]
    assert set(manifest["tolerances"]) == set(CATALOG)
    assert set(manifest["conventions"]) == set(CATALOG)
    assert set(manifest["cases"]) == set(CASES)
    for case in manifest["cases"].values():
        assert set(case["inputs"]) == {"parquet", "csv"}
        assert len(case["expected"]) == len(CATALOG)
        for item in case["expected"].values():
            assert set(item) >= {
                "parquet",
                "csv",
                "columns",
                "stable_from_index",
                "stability_reason",
            }
            assert set(item["stable_from_index"]) == set(item["columns"])
    rw = manifest["cases"]["random_walk"]["expected"]
    assert rw["ema_20"]["stable_from_index"] == {"ema_20": 158}
    assert rw["adx_14"]["stable_from_index"]["adx_14"] == 387
    assert rw["obv"]["stable_from_index"] == {"obv": None}


def test_versioned_files_match_manifest_sha256() -> None:
    manifest = _manifest(PACK)
    listed = set(manifest["files"])
    on_disk = {p.relative_to(PACK).as_posix() for p in PACK.rglob("*") if p.is_file()}
    assert on_disk == listed | {"manifest.json"}
    for rel, digest in manifest["files"].items():
        assert hashlib.sha256((PACK / rel).read_bytes()).hexdigest() == digest, rel


# Tolerancia de regeneración entre máquinas: pocos ULP de float64 relativos a la escala.
# Polars vectoriza distinto según el CPU (SIMD/FMA) y el orden de las sumas cambia el último bit;
# ninguna librería numérica garantiza bytes idénticos entre plataformas.
REGEN_RTOL = 1e-12


def _assert_same_data(fresh: pl.DataFrame, versioned: pl.DataFrame, rel: str) -> None:
    """Estructura exacta (columnas, tipos, nulos, enteros) y flotantes dentro de ``REGEN_RTOL``."""
    assert fresh.columns == versioned.columns, rel
    assert fresh.dtypes == versioned.dtypes, rel
    assert fresh.height == versioned.height, rel
    for name in fresh.columns:
        a, b = fresh[name], versioned[name]
        assert a.is_null().to_list() == b.is_null().to_list(), f"{rel}:{name} (nulos)"
        if not a.dtype.is_float():
            assert a.to_list() == b.to_list(), f"{rel}:{name}"
            continue
        scale = max(float(b.abs().max() or 0.0), 1e-300)
        worst = float((a - b).abs().max() or 0.0)
        assert worst <= REGEN_RTOL * scale, f"{rel}:{name} difiere {worst:.3e} (escala {scale:.3e})"


def test_regeneration_matches_versioned_pack(regenerated: Path) -> None:
    versioned, fresh = _manifest(PACK), _manifest(regenerated)
    assert set(versioned["files"]) == set(fresh["files"])
    for rel in versioned["files"]:
        if rel.endswith(".csv"):
            read = pl.read_csv
            options: dict[str, object] = {"infer_schema_length": None}
        else:
            read = pl.read_parquet
            options = {}
        _assert_same_data(read(regenerated / rel, **options), read(PACK / rel, **options), rel)
    # Igual salvo el commit generador y los hash (el último bit puede variar entre CPUs).
    for manifest in (versioned, fresh):
        manifest["generator"].pop("commit")
        manifest.pop("files")
    assert versioned == fresh


def test_expected_outputs_are_float64() -> None:
    frame = pl.read_parquet(PACK / "expected" / "random_walk" / "macd_12_26_9.parquet")
    assert frame.schema["open_time_ms"] == pl.Int64
    assert all(frame.schema[c] == pl.Float64 for c in frame.columns[1:])


def _parse(cell: str) -> float | None:
    return None if cell == "" else float(cell)


def test_csv_readable_with_stdlib_and_equal_to_parquet() -> None:
    """Formato independiente de Polars: ``csv`` estándar + ``float()`` reproducen los valores."""
    for case, indicator in (("nulls_nan", "rsi_14"), ("random_walk", "bb_20_2")):
        for kind, rel in (
            ("inputs", f"inputs/{case}"),
            ("expected", f"expected/{case}/{indicator}"),
        ):
            with (PACK / f"{rel}.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            header, body = rows[0], rows[1:]
            frame = pl.read_parquet(PACK / f"{rel}.parquet")
            assert header == frame.columns, kind
            assert len(body) == frame.height
            for j, column in enumerate(header):
                parsed = [_parse(r[j]) for r in body]
                expected = frame[column].to_list()
                for got, want in zip(parsed, expected, strict=True):
                    if want is None:
                        assert got is None
                    elif isinstance(want, float) and math.isnan(want):
                        assert got is not None and math.isnan(got)
                    else:
                        assert got == want, (rel, column)
