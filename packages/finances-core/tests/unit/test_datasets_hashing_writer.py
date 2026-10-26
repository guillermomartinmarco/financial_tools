"""T2.2 de ``ft_datasets``: el hash ``ftds-v1`` no depende del escritor Parquet (ADR-D02)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from finances_core.datasets.hashing import STRICT_COLUMNS, hash_frame

ROWS = 5_000
START_MS = 1_704_067_200_000

WRITERS: dict[str, dict[str, Any]] = {
    "zstd-rg1000-stats": {"compression": "zstd", "row_group_size": 1_000, "statistics": True},
    "zstd-rg100000-nostats": {
        "compression": "zstd",
        "row_group_size": 100_000,
        "statistics": False,
    },
    "snappy-rg1000-nostats": {
        "compression": "snappy",
        "row_group_size": 1_000,
        "statistics": False,
    },
    "uncompressed-rg100000-stats": {
        "compression": "uncompressed",
        "row_group_size": 100_000,
        "statistics": True,
    },
}


def _frame() -> pl.DataFrame:
    """Frame binance con bits float32 arbitrarios (incluye NaN, inf, subnormales) y nulos."""
    rng = np.random.default_rng(20240101)
    data: dict[str, pl.Series] = {
        "open_time_ms": pl.Series(START_MS + np.arange(ROWS, dtype=np.int64) * 60_000),
        "trades_count": pl.Series(rng.integers(0, 10_000, ROWS, dtype=np.int64)),
    }
    for column in STRICT_COLUMNS["binance"]:
        if column not in data:
            bits = rng.integers(0, 2**32, ROWS, dtype=np.uint64).astype(np.uint32)
            data[column] = pl.Series(bits.view(np.float32))
    frame = pl.DataFrame(data).select(STRICT_COLUMNS["binance"])
    nulls = rng.choice(ROWS, size=50, replace=False).tolist()
    return frame.with_columns(
        frame["volume"].scatter(nulls, None), frame["trades_count"].scatter(nulls[:10], None)
    )


def test_same_frame_different_writers_same_hash(tmp_path: Path) -> None:
    frame = _frame()
    expected = hash_frame(frame, STRICT_COLUMNS["binance"])
    payloads: dict[str, bytes] = {}
    for name, options in WRITERS.items():
        path = tmp_path / f"{name}.parquet"
        frame.write_parquet(path, **options)
        payloads[name] = path.read_bytes()
        assert hash_frame(pl.read_parquet(path), STRICT_COLUMNS["binance"]) == expected, name
    assert len(set(payloads.values())) == len(WRITERS), "los archivos deberían diferir en bytes"


@pytest.mark.parametrize("split", [1, 999, 2_500])
def test_split_across_files_same_hash(tmp_path: Path, split: int) -> None:
    frame = _frame()
    frame.head(split).write_parquet(tmp_path / "a.parquet", compression="snappy")
    frame.tail(ROWS - split).write_parquet(tmp_path / "b.parquet", compression="zstd")
    reread = pl.read_parquet([tmp_path / "a.parquet", tmp_path / "b.parquet"])
    assert hash_frame(reread, STRICT_COLUMNS["binance"]) == hash_frame(
        frame, STRICT_COLUMNS["binance"]
    )
