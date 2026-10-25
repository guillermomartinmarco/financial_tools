"""T3.2/T3.3 — Escritura Parquet Hive con merge sin pérdida (RF-4)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
import pytest

from finances_downloader.ft_market_data_binance.inventory import inventory_local_parquet
from finances_downloader.ft_market_data_binance.normalizer import normalize_rest_json
from finances_downloader.ft_market_data_binance.writer import write_parquet_partitioned

from .conftest import kline_row, ms

H = 3_600_000
BASE = "binance/market_type=spot/symbol=BTCUSDT/timeframe=1h"


def _df(start: int, n: int, price: float = 100.0, symbol: str = "BTCUSDT") -> pl.DataFrame:
    rows = [kline_row(start + i * H, price=price) for i in range(n)]
    return normalize_rest_json(rows, symbol, "1h", "spot", start, start + n * H)


def _hash(paths: list[str]) -> dict[str, str]:
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(paths)}


def test_writes_full_hive_layout(tmp_path: Path) -> None:
    paths = write_parquet_partitioned(_df(ms(2024, 1, 31, 23), 2), tmp_path)
    assert sorted(paths) == sorted(
        [
            str(tmp_path / f"{BASE}/year=2024/month=01/data.parquet"),
            str(tmp_path / f"{BASE}/year=2024/month=02/data.parquet"),
        ]
    )
    lf = pl.scan_parquet(tmp_path / "binance" / "**" / "*.parquet", hive_partitioning=True)
    df = lf.collect()
    hive = {"market_type", "symbol", "timeframe", "year", "month"}
    assert set(df.columns) >= {*hive, "open_time_ms"}
    assert df.height == 2


def test_file_schema_float32_int64_zstd_statistics(tmp_path: Path) -> None:
    [path] = write_parquet_partitioned(_df(ms(2024, 1, 1), 3), tmp_path)
    schema = pl.read_parquet_schema(path)
    assert schema["open_time_ms"] == pl.Int64
    assert schema["trades_count"] == pl.Int64
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        assert schema[col] == pl.Float32
    meta = pq.ParquetFile(path).metadata
    column = meta.row_group(0).column(0)
    assert column.compression == "ZSTD"
    assert column.statistics is not None


def test_merge_keeps_existing_rows_outside_new_range(tmp_path: Path) -> None:
    """Bug #1: una re-ejecución parcial sobrescribía el mes completo con menos filas."""
    write_parquet_partitioned(_df(ms(2024, 1, 1), 48), tmp_path)
    [path] = write_parquet_partitioned(_df(ms(2024, 1, 2), 5, price=500.0), tmp_path)
    df = pl.read_parquet(path)
    assert df.height == 48
    assert df["open_time_ms"].is_sorted()
    assert df["open_time_ms"].n_unique() == 48
    # La fila nueva prevalece sobre la existente
    replaced = df.filter(pl.col("open_time_ms") == ms(2024, 1, 2))
    assert replaced["open"][0] == pytest.approx(500.0)
    untouched = df.filter(pl.col("open_time_ms") == ms(2024, 1, 1))
    assert untouched["open"][0] == pytest.approx(100.0)


def test_merge_extends_partition(tmp_path: Path) -> None:
    write_parquet_partitioned(_df(ms(2024, 1, 1), 5), tmp_path)
    [path] = write_parquet_partitioned(_df(ms(2024, 1, 1, 5), 5), tmp_path)
    assert pl.read_parquet(path).height == 10


def test_atomic_replace_leaves_no_temp_files(tmp_path: Path) -> None:
    write_parquet_partitioned(_df(ms(2024, 1, 1), 5), tmp_path)
    write_parquet_partitioned(_df(ms(2024, 1, 1), 5), tmp_path)
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file() and p.name != "data.parquet"]
    assert leftovers == []


def test_failed_write_keeps_previous_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [path] = write_parquet_partitioned(_df(ms(2024, 1, 1), 5), tmp_path)
    before = Path(path).read_bytes()

    def boom(self: Path, target: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_parquet_partitioned(_df(ms(2024, 1, 1, 5), 5), tmp_path)
    assert Path(path).read_bytes() == before
    assert [p.name for p in Path(path).parent.iterdir()] == ["data.parquet"]


def test_empty_dataframe_writes_nothing(tmp_path: Path) -> None:
    assert write_parquet_partitioned(_df(ms(2024, 1, 1), 0), tmp_path) == []


def test_idempotent_double_run_same_files_rows_and_hashes(tmp_path: Path) -> None:
    """T3.3: normalizer + writer + inventory, 2 ejecuciones idénticas."""
    df = _df(ms(2024, 1, 31, 20), 10)
    first = write_parquet_partitioned(df, tmp_path)
    hashes_1 = _hash(first)
    rows_1 = sum(pl.read_parquet(p).height for p in first)
    inv_1 = inventory_local_parquet(tmp_path, "BTCUSDT", "1h", "spot")

    second = write_parquet_partitioned(df, tmp_path)
    assert sorted(second) == sorted(first)
    assert sum(pl.read_parquet(p).height for p in second) == rows_1 == 10
    assert _hash(second) == hashes_1
    assert inventory_local_parquet(tmp_path, "BTCUSDT", "1h", "spot") == inv_1
