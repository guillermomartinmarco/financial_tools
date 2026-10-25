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


# --- T1.1 (ft_indicators): ``file_name`` y ``mode`` retrocompatibles -------------------

#: Esquema de una serie de indicador: ``open_time_ms`` + salidas (sin columnas de identidad).
SERIES_SCHEMA: dict[str, type[pl.DataType]] = {
    "market_type": pl.Utf8,
    "symbol": pl.Utf8,
    "timeframe": pl.Utf8,
    "open_time_ms": pl.Int64,
    "rsi_14": pl.Float32,
}


def series_frame(start_ms: int, rows: int, value: float) -> pl.DataFrame:
    base = frame(start_ms, rows)
    return base.select(
        "market_type", "symbol", "timeframe", "open_time_ms", "year", "month"
    ).with_columns(pl.lit(value).alias("rsi_14"))


def test_custom_file_name(tmp_path: Path) -> None:
    written = write_parquet_partitioned(
        series_frame(ms(2024, 1, 1), 3, 50.0),
        tmp_path,
        exchange="binance",
        schema=SERIES_SCHEMA,
        file_name="rsi_14.parquet",
    )
    part = partition_dir(tmp_path, "binance", ("equity", "AAPL", "1h", 2024, 1))
    assert written == [str(part / "rsi_14.parquet")]
    assert not (part / "data.parquet").exists()
    df = pl.read_parquet(part / "rsi_14.parquet")
    assert df.columns == ["open_time_ms", "rsi_14"]
    assert df.schema["rsi_14"] == pl.Float32


def test_replace_mode_discards_previous_month_rows(tmp_path: Path) -> None:
    kwargs = {"exchange": "binance", "schema": SERIES_SCHEMA, "file_name": "rsi_14.parquet"}
    write_parquet_partitioned(series_frame(ms(2024, 1, 1), 10, 1.0), tmp_path, **kwargs)
    write_parquet_partitioned(
        series_frame(ms(2024, 1, 1, 5), 2, 2.0),
        tmp_path,
        mode="replace",
        **kwargs,
    )
    part = partition_dir(tmp_path, "binance", ("equity", "AAPL", "1h", 2024, 1))
    df = pl.read_parquet(part / "rsi_14.parquet")
    assert df["open_time_ms"].to_list() == [ms(2024, 1, 1, 5), ms(2024, 1, 1, 6)]
    assert df["rsi_14"].to_list() == [2.0, 2.0]


def test_replace_mode_sorts_rows(tmp_path: Path) -> None:
    df = series_frame(ms(2024, 1, 1), 4, 1.0).reverse()
    write_parquet_partitioned(
        df,
        tmp_path,
        exchange="binance",
        schema=SERIES_SCHEMA,
        file_name="x.parquet",
        mode="replace",
    )
    part = partition_dir(tmp_path, "binance", ("equity", "AAPL", "1h", 2024, 1))
    times = pl.read_parquet(part / "x.parquet")["open_time_ms"].to_list()
    assert times == sorted(times)


def test_merge_is_default_mode(tmp_path: Path) -> None:
    write_parquet_partitioned(
        frame(ms(2024, 1, 1), 3), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    write_parquet_partitioned(
        frame(ms(2024, 1, 1, 3), 2), tmp_path, exchange="yfinance", schema=SCHEMA
    )
    part = partition_dir(tmp_path, "yfinance", ("equity", "AAPL", "1h", 2024, 1))
    assert pl.read_parquet(part / "data.parquet").height == 5


def test_unknown_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mode"):
        write_parquet_partitioned(
            frame(ms(2024, 1, 1), 1),
            tmp_path,
            exchange="x",
            schema=SCHEMA,
            mode="append",
        )
