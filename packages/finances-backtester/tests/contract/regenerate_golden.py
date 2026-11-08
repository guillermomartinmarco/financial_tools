"""Regenera los *golden* de la suite de contrato (B6.1, plan §5).

Uso, desde ``packages/finances-backtester``::

    python -m tests.contract.regenerate_golden

Escribe en ``tests/contract/golden/`` (o en el directorio dado como primer argumento),
para cada estrategia integrada, ``signals_<kind>.json``, ``trades_<kind>.parquet``,
``equity_<kind>.parquet`` y ``summary_<kind>.json``, más ``fixture.json`` (semilla,
parámetros, costes y huella de las velas). Todo sale de la referencia independiente
(``reference.py``) sobre el fixture (``fixture.py``); la salida es determinista y byte a
byte idéntica entre ejecuciones con las mismas versiones de Polars. Cualquier cambio de
los *golden* exige revisión en el PR.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

import polars as pl

from tests.contract.fixture import (
    FIXTURE_BARS,
    FIXTURE_SEED,
    GOLDEN_COSTS,
    GOLDEN_PARAMS,
    KINDS,
    TIE_MARGIN,
    TIMEFRAME,
    candles_sha256,
    fixture_candles,
)
from tests.contract.golden_io import GOLDEN_DIR
from tests.contract.reference import (
    equity_frame,
    position_edges,
    reference_position,
    reference_signals,
    simulate,
    summarize,
    threshold_margin,
    trades_frame,
)

PARQUET_COMPRESSION: Final = "zstd"
PARQUET_LEVEL = 3


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_bytes(text.encode("utf-8"))


def _write_parquet(path: Path, frame: pl.DataFrame) -> None:
    frame.write_parquet(
        path,
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_LEVEL,
        statistics=False,
        row_group_size=1_000_000,
    )


def golden_files() -> list[str]:
    """Nombres de todos los ficheros *golden*, en orden estable."""
    names = ["fixture.json"]
    for kind in KINDS:
        names += [
            f"signals_{kind}.json",
            f"trades_{kind}.parquet",
            f"equity_{kind}.parquet",
            f"summary_{kind}.json",
        ]
    return names


def write_golden(out_dir: Path = GOLDEN_DIR) -> list[Path]:
    """Escribe los *golden* en ``out_dir`` y devuelve las rutas escritas."""
    out_dir.mkdir(parents=True, exist_ok=True)
    candles = fixture_candles()
    fixture: dict[str, Any] = {
        "seed": FIXTURE_SEED,
        "n_bars": FIXTURE_BARS,
        "timeframe": TIMEFRAME,
        "start_ms": candles.open_time_ms[0],
        "end_ms": candles.open_time_ms[-1],
        "candles_sha256": candles_sha256(candles),
        "tie_margin": TIE_MARGIN,
        "costs": {
            "initial_cash": GOLDEN_COSTS.initial_cash,
            "size_fraction": GOLDEN_COSTS.size_fraction,
            "fees_bps": GOLDEN_COSTS.fees_bps,
            "slippage_bps": GOLDEN_COSTS.slippage_bps,
        },
        "params": GOLDEN_PARAMS,
        "threshold_margin": {},
    }
    written: list[Path] = []
    for kind in KINDS:
        params = GOLDEN_PARAMS[kind]
        entries, exits = reference_signals(kind, candles, params)
        # Golden en posición-objetivo (L0-G2a): se guardan los flancos de la posición
        # sostenida (enter/exit de +1), no los eventos crudos de RF-2.
        enter_edges, exit_edges = position_edges(reference_position(entries, exits))
        margin, bar = threshold_margin(kind, candles, params)
        fixture["threshold_margin"][kind] = {"min": margin, "bar": bar}
        _write_json(
            out_dir / f"signals_{kind}.json",
            {
                "kind": kind,
                "params": params,
                "n_bars": candles.n,
                "entries": [t for t, value in enumerate(enter_edges) if value],
                "exits": [t for t, value in enumerate(exit_edges) if value],
            },
        )
        sim = simulate(candles, entries, exits, GOLDEN_COSTS)
        _write_parquet(out_dir / f"trades_{kind}.parquet", trades_frame(sim))
        _write_parquet(out_dir / f"equity_{kind}.parquet", equity_frame(candles, sim))
        summary = summarize(
            sim, candles, initial_cash=GOLDEN_COSTS.initial_cash, timeframe=TIMEFRAME
        )
        _write_json(out_dir / f"summary_{kind}.json", summary)
    _write_json(out_dir / "fixture.json", fixture)
    for name in golden_files():
        written.append(out_dir / name)
    return written


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]) if argv else GOLDEN_DIR
    for path in write_golden(out_dir):
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
