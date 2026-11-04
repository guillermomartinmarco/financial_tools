"""Lectura de los *golden* versionados de la suite de contrato (B6.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def load_json(name: str, golden_dir: Path = GOLDEN_DIR) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((golden_dir / name).read_text(encoding="utf-8"))
    return payload


def golden_signals(kind: str, golden_dir: Path = GOLDEN_DIR) -> tuple[list[bool], list[bool]]:
    """``entries``/``exits`` de ``signals_<kind>.json`` como listas booleanas completas."""
    payload = load_json(f"signals_{kind}.json", golden_dir)
    n = int(payload["n_bars"])
    entries, exits = [False] * n, [False] * n
    for t in payload["entries"]:
        entries[t] = True
    for t in payload["exits"]:
        exits[t] = True
    return entries, exits


def golden_table(name: str, kind: str, golden_dir: Path = GOLDEN_DIR) -> pl.DataFrame:
    """``trades_<kind>.parquet`` o ``equity_<kind>.parquet``."""
    return pl.read_parquet(golden_dir / f"{name}_{kind}.parquet")
