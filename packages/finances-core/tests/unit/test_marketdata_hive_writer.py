"""T1.1: escritor Hive del núcleo, parametrizado por ``exchange`` y esquema (RF-5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from finances_core.marketdata.hive_writer import (
    HIVE_KEYS,
    data_columns,
    partition_dir,
    write_parquet_partitioned,
)

HOUR_MS = 3_600_000

#: Esquema estilo Yahoo (incluye ``adj_close``), independiente del de Binance.
SCHEMA: dict[str, type[pl.DataType]] = {
    "symbol": pl.Utf8,
    "timeframe": pl.Utf8,
    "market_type": pl.Utf8,
    "open_time_ms": pl.Int64,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,
    "adj_close": pl.Float32,
    "volume": pl.Float32,
}


def ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def frame(start_ms: int, rows: int, *, price: float = 1.0, symbol: str = "AAPL") -> pl.DataFrame:
    times = [start_ms + i * HOUR_MS for i in range(rows)]
    dts = [datetime.fromtimestamp(t / 1000, tz=UTC) for t in times]
    return pl.DataFrame(
        {
            "symbol": [symbol] * rows,
            "timeframe": ["1h"] * rows,
            "market_type": ["equity"] * rows,
            "open_time_ms": times,
            "open": [price] * rows,
            "high": [price] * rows,
            "low": [price] * rows,
            "close": [price] * rows,
            "adj_close": [price] * rows,
            "volume": [10.0] * rows,
            "year": [d.year for d in dts],
            "month": [d.month for d in dts],
        },
        schema_overrides={"open_time_ms": pl.Int64, "year": pl.Int32, "month": pl.Int32},
    )


def test_data_columns_exclude_hive_keys() -> None:
    cols = data_columns(SCHEMA)
    assert cols == ["open_time_ms", "open", "high", "low", "close", "adj_close", "volume"]
    assert not set(cols) & set(HIVE_KEYS)


def test_partition_dir_is_parametrized_by_exchange(tmp_path: Path) -> None:
    path = partition_dir(tmp_path, "yfinance", ("index", "^GSPC", "1d", 2024, 1))
    assert path == (
        tmp_path
        / "yfinance"
        / "market_type=index"
        / "symbol=^GSPC"
        / "timeframe=1d"
        / "year=2024"
        / "month=01"
    )


def test_write_uses_exchange_layout_and_schema_columns(tmp_path: Path) -> None:
    written = write_parquet_partitioned(
        frame(ms(2024, 1, 31, 22), 4), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    base = tmp_path / "yfinance" / "market_type=equity" / "symbol=AAPL" / "timeframe=1h"
    assert [Path(p) for p in written] == [
        base / "year=2024" / "month=01" / "data.parquet",
        base / "year=2024" / "month=02" / "data.parquet",
    ]
    df = pl.read_parquet(written[0], hive_partitioning=False)
    assert df.columns == data_columns(SCHEMA)
    assert df.schema["adj_close"] == pl.Float32
    assert df.schema["open_time_ms"] == pl.Int64
    assert df.height == 2
    assert not (tmp_path / "binance").exists()


def test_narrower_rerun_keeps_existing_rows(tmp_path: Path) -> None:
    """Merge sin pérdida: re-ejecución con rango menor no borra filas fuera del rango."""
    [path] = write_parquet_partitioned(
        frame(ms(2024, 3, 1), 10), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    write_parquet_partitioned(
        frame(ms(2024, 3, 1, 3), 2), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    df = pl.read_parquet(path, hive_partitioning=False)
    assert df.height == 10
    assert df["open_time_ms"].is_sorted()


def test_dedupe_keeps_newest_row(tmp_path: Path) -> None:
    [path] = write_parquet_partitioned(
        frame(ms(2024, 3, 1), 3, price=1.0), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    write_parquet_partitioned(
        frame(ms(2024, 3, 1, 1), 1, price=9.0), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    df = pl.read_parquet(path, hive_partitioning=False)
    assert df["close"].to_list() == [1.0, 9.0, 1.0]


def test_duplicates_inside_batch_keep_last(tmp_path: Path) -> None:
    batch = pl.concat([frame(ms(2024, 3, 1), 2, price=1.0), frame(ms(2024, 3, 1), 1, price=5.0)])
    [path] = write_parquet_partitioned(batch, tmp_path, exchange="yfinance", schema=SCHEMA)
    df = pl.read_parquet(path, hive_partitioning=False)
    assert df["close"].to_list() == [5.0, 1.0]


def test_failed_atomic_write_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [path] = write_parquet_partitioned(
        frame(ms(2024, 3, 1), 3), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    before = Path(path).read_bytes()

    def boom(self: Path, target: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_parquet_partitioned(
            frame(ms(2024, 3, 1, 5), 3), tmp_path, exchange="yfinance", schema=SCHEMA
        )
    assert Path(path).read_bytes() == before
    assert [p.name for p in Path(path).parent.iterdir()] == ["data.parquet"]


def test_empty_frame_writes_nothing(tmp_path: Path) -> None:
    assert write_parquet_partitioned(frame(0, 0), tmp_path, exchange="x", schema=SCHEMA) == []
    assert list(tmp_path.iterdir()) == []
