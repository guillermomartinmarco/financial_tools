"""T6.1, T6.2: ``service.run_import`` (RF-4, RF-5) sobre un almacén Parquet temporal.

Todos los orígenes viven en ``tmp_path`` (fuera del almacén) y todo el almacén es temporal.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.export import stream_export
from structlog.testing import capture_logs

from finances_downloader.ft_market_data_manager import planner, service
from finances_downloader.ft_market_data_manager.errors import (
    ConfirmationRequiredError,
    ConflictError,
    DatasetNotFoundError,
    ImportValidationError,
    InvalidArgumentError,
)

from .conftest import (
    FEB_2024_MS,
    HOUR_MS,
    JAN_2024_MS,
    dataset_root,
    hours,
    make_ohlcv,
    write_partition_files,
)
from .import_helpers import (
    FullFormat,
    hourly,
    import_request,
    make_binance,
    make_yf,
    read_dataset,
    tree_fingerprint,
    write_source,
)

DAY_MS = 24 * HOUR_MS


@pytest.fixture(autouse=True)
def _tmp_store_only(parquet_dir: Path, tmp_path: Path) -> None:
    """Seguridad: ningún test de este módulo toca el almacén real."""
    assert parquet_dir == FinancesSettings().PARQUET_DIR
    assert parquet_dir.is_relative_to(tmp_path)


@pytest.fixture
def incoming(tmp_path: Path) -> Path:
    return tmp_path / "incoming"


@pytest.fixture
def store(parquet_dir: Path) -> Path:
    """binance/spot/BTCUSDT/1h con enero (6 velas) y febrero (4 velas), esquema de 7 columnas."""
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 6))])
    write_partition_files(parquet_dir, 2024, 2, [make_ohlcv(hours(FEB_2024_MS, 4))])
    return parquet_dir


def _ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def _root(parquet_dir: Path, **ds: str) -> Path:
    return dataset_root(parquet_dir, **ds)


# --- T6.1: columnas ----------------------------------------------------------------------


def test_missing_columns_rejected_without_writing(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv(hourly()).drop("volume"), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match="faltan columnas.*volume"):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


def test_extra_columns_rejected_without_writing(store: Path, incoming: Path) -> None:
    df = make_ohlcv(hourly()).with_columns(extra=pl.lit(1.0))
    src = write_source(incoming, df, "csv")
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match="sobran columnas.*extra"):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


def test_symbol_timeframe_market_type_columns_are_extra(store: Path, incoming: Path) -> None:
    df = make_ohlcv(hourly()).with_columns(symbol=pl.lit("BTCUSDT"))
    with pytest.raises(ImportValidationError, match="sobran columnas.*symbol"):
        service.run_import(import_request(write_source(incoming, df, "parquet")))


# --- T6.1: timestamp ---------------------------------------------------------------------


def test_seconds_read_as_milliseconds_rejected_with_hint(store: Path, incoming: Path) -> None:
    seconds = [t // 1000 for t in hourly()]
    src = write_source(incoming, make_ohlcv(seconds), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match=r"segundos.*--ts-unit s"):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


def test_ts_unit_s_converts_to_milliseconds(store: Path, incoming: Path) -> None:
    times = hours(JAN_2024_MS + 10 * HOUR_MS, 3)
    df = make_ohlcv([t // 1000 for t in times]).rename({"open_time_ms": "time"})
    src = write_source(incoming, df, "csv")
    result = service.run_import(import_request(src, ts_column="time", ts_unit="s"))
    assert result.import_plan is not None
    assert result.import_plan.rows_new == 3
    stored = read_dataset(_root(store))["open_time_ms"].to_list()
    assert all(t in stored for t in times)


@pytest.mark.parametrize(("unit", "factor"), [("us", 1_000), ("ns", 1_000_000)])
def test_ts_unit_us_ns_exact_conversion(
    store: Path, incoming: Path, unit: str, factor: int
) -> None:
    times = hours(JAN_2024_MS + 10 * HOUR_MS, 2)
    src = write_source(incoming, make_ohlcv([t * factor for t in times]), "parquet")
    result = service.run_import(import_request(src, ts_unit=unit))
    assert result.import_plan is not None
    assert result.import_plan.range_ms == (times[0], times[-1])


def test_submillisecond_precision_is_rejected(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv([t * 1_000 + 1 for t in hourly()]), "parquet")
    with pytest.raises(ImportValidationError, match="sub-milisegundo"):
        service.run_import(import_request(src, ts_unit="us"))


def test_far_future_timestamps_rejected(store: Path, incoming: Path) -> None:
    year_2100 = _ms(2100, 1, 1)
    src = write_source(incoming, make_ohlcv([year_2100]), "parquet")
    with pytest.raises(ImportValidationError, match="plausible|rango"):
        service.run_import(import_request(src))


def test_timestamps_before_2000_rejected(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv([_ms(1999, 12, 31, 23)]), "parquet")
    with pytest.raises(ImportValidationError, match="plausible|rango"):
        service.run_import(import_request(src))


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_iso_timestamps_rejected(store: Path, incoming: Path, fmt: FullFormat) -> None:
    df = make_ohlcv(hourly()).with_columns(open_time_ms=pl.Series(["2024-01-01T00:00:00Z"] * 4))
    src = write_source(incoming, df, fmt)
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match="ISO-8601"):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


def test_text_timestamp_column_in_parquet_rejected(store: Path, incoming: Path) -> None:
    df = make_ohlcv(hourly()).with_columns(open_time_ms=pl.Series(["2024-01-01"] * 4))
    src = write_source(incoming, df, "parquet")
    with pytest.raises(ImportValidationError, match="debe ser entera"):
        service.run_import(import_request(src))


def test_shifted_by_three_hours_rejected_for_alignment(store: Path, incoming: Path) -> None:
    shifted = [t + 3 * HOUR_MS // 6 for t in hourly()]  # +30 min: fuera de la grilla de 1h
    src = write_source(incoming, make_ohlcv(shifted), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match=r"4 filas desalineadas.*ejemplo"):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


def test_shifted_daily_candles_plus_three_hours_rejected(parquet_dir: Path, incoming: Path) -> None:
    days = [_ms(2024, 1, d, 3) for d in (1, 2, 3)]
    src = write_source(incoming, make_binance(days), "parquet")
    before = tree_fingerprint(parquet_dir)
    with pytest.raises(ImportValidationError, match="3 filas desalineadas"):
        service.run_import(import_request(src, timeframe="1d", create=True))
    assert tree_fingerprint(parquet_dir) == before


@pytest.mark.parametrize(
    ("timeframe", "good", "bad"),
    [
        ("1d", [_ms(2024, 1, 1), _ms(2024, 1, 2)], _ms(2024, 1, 2, 1)),
        ("1w", [_ms(2024, 1, 1), _ms(2024, 1, 8)], _ms(2024, 1, 2)),
        ("1M", [_ms(2024, 1, 1), _ms(2024, 2, 1)], _ms(2024, 2, 2)),
    ],
)
def test_calendar_grids(
    parquet_dir: Path, incoming: Path, timeframe: str, good: list[int], bad: int
) -> None:
    ok = write_source(incoming, make_binance(good), "parquet", "ok")
    plan = service.run_import(import_request(ok, timeframe=timeframe, create=True)).import_plan
    assert plan is not None
    assert plan.rows_new == len(good)
    assert plan.range_ms == (good[0], good[-1])
    ko = write_source(incoming, make_binance([*good, bad]), "parquet", "ko")
    with pytest.raises(ImportValidationError, match="1 filas desalineadas"):
        service.run_import(import_request(ko, timeframe=timeframe, create=True))


def test_unknown_timeframe_rejected(parquet_dir: Path, incoming: Path) -> None:
    src = write_source(incoming, make_binance(hourly()), "parquet")
    with pytest.raises(InvalidArgumentError, match="timeframe"):
        service.run_import(import_request(src, timeframe="7x", create=True))


def test_duplicates_in_source_rejected_with_examples(store: Path, incoming: Path) -> None:
    times = hourly()
    src = write_source(incoming, make_ohlcv([*times, times[1], times[1], times[2]]), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match=r"duplicad.*ejemplo.*" + str(times[1])):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


def test_empty_file_rejected(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv([]), "parquet")
    with pytest.raises(ImportValidationError, match="no hay filas"):
        service.run_import(import_request(src))


# --- T6.1: valores -----------------------------------------------------------------------


def _bad_values(times: list[int]) -> pl.DataFrame:
    df = make_ohlcv(times)
    return df.with_columns(
        close=pl.Series([100.0, -1.0, 100.0, 100.0], dtype=pl.Float32),
        volume=pl.Series([10.0, 10.0, None, 10.0], dtype=pl.Float32),
    )


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_invalid_rows_rejected_by_default(store: Path, incoming: Path, fmt: FullFormat) -> None:
    src = write_source(incoming, _bad_values(hourly(JAN_2024_MS + 20 * HOUR_MS)), fmt)
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match="2 filas inválidas"):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_skip_invalid_imports_valid_rows_and_reports_rejections(
    store: Path, incoming: Path, fmt: FullFormat
) -> None:
    times = hourly(JAN_2024_MS + 20 * HOUR_MS)
    src = write_source(incoming, _bad_values(times), fmt)
    plan = service.run_import(import_request(src, skip_invalid=True)).import_plan
    assert plan is not None
    assert (plan.rows_in_file, plan.rows_new, plan.rows_rejected) == (4, 2, 2)
    assert len(plan.rejected_examples) == 2
    stored = read_dataset(_root(store))["open_time_ms"].to_list()
    assert times[0] in stored
    assert times[3] in stored
    assert times[1] not in stored
    assert times[2] not in stored


def test_zero_price_and_negative_volume_are_invalid(store: Path, incoming: Path) -> None:
    times = hourly(JAN_2024_MS + 20 * HOUR_MS, 2)
    df = make_ohlcv(times).with_columns(
        open=pl.Series([0.0, 100.0], dtype=pl.Float32),
        volume=pl.Series([10.0, -0.5], dtype=pl.Float32),
    )
    src = write_source(incoming, df, "parquet")
    with pytest.raises(ImportValidationError, match="2 filas inválidas"):
        service.run_import(import_request(src))


def test_zero_volume_is_valid(store: Path, incoming: Path) -> None:
    times = hourly(JAN_2024_MS + 20 * HOUR_MS, 2)
    df = make_ohlcv(times).with_columns(volume=pl.lit(0.0, dtype=pl.Float32))
    plan = service.run_import(import_request(write_source(incoming, df, "csv"))).import_plan
    assert plan is not None
    assert plan.rows_new == 2


def test_nan_and_inf_in_csv_are_invalid(store: Path, incoming: Path) -> None:
    times = hourly(JAN_2024_MS + 20 * HOUR_MS, 2)
    path = incoming / "n.csv"
    incoming.mkdir()
    lines = ["open_time_ms,open,high,low,close,volume,trades_count"]
    lines += [f"{times[0]},nan,1,1,1,1,1", f"{times[1]},1,1,1,inf,1,1"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ImportValidationError, match="2 filas inválidas"):
        service.run_import(import_request(path))


# --- T6.2: origen y destino --------------------------------------------------------------


def test_missing_source_is_not_found(store: Path, incoming: Path) -> None:
    with pytest.raises(DatasetNotFoundError, match="origen"):
        service.run_import(import_request(incoming / "nope.parquet"))


def test_source_inside_store_rejected(store: Path) -> None:
    inside = store / "binance" / "outside.parquet"
    make_ohlcv(hourly()).write_parquet(inside)
    with pytest.raises(InvalidArgumentError, match="FT_PARQUET_DIR|almacén"):
        service.run_import(import_request(inside))


def test_unknown_extension_needs_format(store: Path, incoming: Path) -> None:
    incoming.mkdir()
    path = incoming / "data.txt"
    make_ohlcv(hourly(JAN_2024_MS + 20 * HOUR_MS)).write_csv(path)
    with pytest.raises(InvalidArgumentError, match="formato"):
        service.run_import(import_request(path))
    plan = service.run_import(import_request(path, format="csv")).import_plan
    assert plan is not None
    assert plan.rows_new == 4


def test_missing_destination_without_create_is_not_found(parquet_dir: Path, incoming: Path) -> None:
    src = write_source(incoming, make_binance(hourly()), "parquet")
    before = tree_fingerprint(parquet_dir)
    with pytest.raises(DatasetNotFoundError, match="--create"):
        service.run_import(import_request(src))
    assert tree_fingerprint(parquet_dir) == before


def test_create_builds_partitions_per_month(parquet_dir: Path, incoming: Path) -> None:
    times = [FEB_2024_MS - HOUR_MS, FEB_2024_MS, FEB_2024_MS + HOUR_MS]
    src = write_source(incoming, make_binance(times), "parquet")
    result = service.run_import(import_request(src, create=True))
    plan = result.import_plan
    assert plan is not None
    assert plan.destination == "binance/spot/BTCUSDT/1h"
    assert plan.partitions_new == ["2024-01", "2024-02"]
    assert plan.partitions_modified == []
    assert (plan.rows_in_file, plan.rows_new, plan.rows_conflict, plan.rows_rejected) == (
        3,
        3,
        0,
        0,
    )
    assert plan.range_ms == (times[0], times[-1])
    assert plan.max_close_divergence is None
    root = _root(parquet_dir)
    jan = pl.read_parquet(root / "year=2024" / "month=01" / "data.parquet")
    feb = pl.read_parquet(root / "year=2024" / "month=02" / "data.parquet")
    assert jan["open_time_ms"].to_list() == [times[0]]
    assert feb["open_time_ms"].to_list() == times[1:]
    assert jan.schema == make_binance([]).schema
    assert {r.result for r in result.partitions} == {"changed"}


def test_invalid_destination_values_rejected(parquet_dir: Path, incoming: Path) -> None:
    src = write_source(incoming, make_binance(hourly()), "parquet")
    for field, value in (("symbol", "../evil"), ("exchange", "a/b"), ("market_type", "nope")):
        with pytest.raises(InvalidArgumentError):
            service.run_import(import_request(src, create=True, **{field: value}))
    assert list(parquet_dir.iterdir()) == []


def test_unknown_exchange_cannot_be_created(parquet_dir: Path, incoming: Path) -> None:
    src = write_source(incoming, make_binance(hourly()), "parquet")
    with pytest.raises(ImportValidationError, match="esquema canónico"):
        service.run_import(import_request(src, create=True, exchange="kraken"))


def test_yfinance_destination_uses_its_schema(parquet_dir: Path, incoming: Path) -> None:
    times = [_ms(2024, 1, 2), _ms(2024, 1, 3)]
    src = write_source(incoming, make_yf(times), "csv")
    request = import_request(
        src, exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d", create=True
    )
    plan = service.run_import(request).import_plan
    assert plan is not None
    assert plan.destination == "yfinance/equity/AAPL/1d"
    stored = pl.read_parquet(
        _root(parquet_dir, exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")
        / "year=2024"
        / "month=01"
        / "data.parquet"
    )
    assert stored.schema == make_yf([]).schema
    # el esquema binance no vale para yfinance
    bad = write_source(incoming, make_binance(times), "csv", "bad")
    with pytest.raises(ImportValidationError, match="sobran columnas"):
        service.run_import(
            import_request(
                bad,
                exchange="yfinance",
                market_type="equity",
                symbol="AAPL",
                timeframe="1d",
                create=True,
            )
        )


# --- T6.2: conflictos --------------------------------------------------------------------


def _existing_rows(store: Path) -> pl.DataFrame:
    return read_dataset(_root(store))


def test_skip_keeps_existing_rows_and_adds_new(store: Path, incoming: Path) -> None:
    before = _existing_rows(store)
    times = hours(JAN_2024_MS + 4 * HOUR_MS, 4)  # h4,h5 existen; h6,h7 son nuevas
    src = write_source(incoming, make_ohlcv(times, close_offset=50.0), "parquet")
    result = service.run_import(import_request(src))
    plan = result.import_plan
    assert plan is not None
    assert (plan.rows_new, plan.rows_conflict) == (2, 2)
    assert plan.partitions_modified == ["2024-01"]
    assert plan.max_close_divergence is None or plan.max_close_divergence >= 0
    after = _existing_rows(store)
    assert after.height == before.height + 2
    kept = after.filter(pl.col("open_time_ms").is_in(before["open_time_ms"].implode()))
    assert kept.equals(before)  # filas existentes idénticas (incluidos los conflictos)
    assert after["open_time_ms"].to_list() == sorted(after["open_time_ms"].to_list())


def test_skip_with_only_conflicts_leaves_partition_untouched(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv(hours(JAN_2024_MS, 3), close_offset=9.0), "parquet")
    before = tree_fingerprint(store)
    result = service.run_import(import_request(src))
    assert tree_fingerprint(store) == before
    assert [r.result for r in result.partitions] == ["unchanged"]
    assert result.import_plan is not None
    assert result.import_plan.partitions_modified == []


def test_reimport_is_idempotent(parquet_dir: Path, incoming: Path) -> None:
    src = write_source(incoming, make_binance(hourly()), "parquet")
    service.run_import(import_request(src, create=True))
    before = tree_fingerprint(parquet_dir)
    again = service.run_import(import_request(src))
    assert tree_fingerprint(parquet_dir) == before
    assert [r.result for r in again.partitions] == ["unchanged"]


def test_fail_aborts_when_there_are_conflicts(store: Path, incoming: Path) -> None:
    times = hours(JAN_2024_MS + 4 * HOUR_MS, 4)
    src = write_source(incoming, make_ohlcv(times), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ConflictError, match="2 conflictos"):
        service.run_import(import_request(src, on_conflict="fail"))
    assert tree_fingerprint(store) == before


def test_fail_without_conflicts_imports(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "parquet")
    plan = service.run_import(import_request(src, on_conflict="fail")).import_plan
    assert plan is not None
    assert plan.rows_new == 2


def test_replace_changes_only_values_of_matching_timestamps(store: Path, incoming: Path) -> None:
    before = _existing_rows(store)
    conflict_ts = [JAN_2024_MS + 2 * HOUR_MS, JAN_2024_MS + 3 * HOUR_MS]
    new_ts = JAN_2024_MS + 9 * HOUR_MS
    df = make_ohlcv([*conflict_ts, new_ts], close_offset=0.5)  # ~0,5 % de divergencia
    plan = service.run_import(
        import_request(write_source(incoming, df, "parquet"), on_conflict="replace")
    ).import_plan
    assert plan is not None
    assert (plan.rows_new, plan.rows_conflict) == (1, 2)
    assert plan.max_close_divergence is not None
    assert 0.004 < plan.max_close_divergence < 0.006
    after = _existing_rows(store)
    assert after.height == before.height + 1
    untouched = [t for t in before["open_time_ms"].to_list() if t not in conflict_ts]
    assert after.filter(pl.col("open_time_ms").is_in(untouched)).equals(
        before.filter(pl.col("open_time_ms").is_in(untouched))
    )
    replaced = after.filter(pl.col("open_time_ms").is_in(conflict_ts))
    assert replaced.equals(df.filter(pl.col("open_time_ms").is_in(conflict_ts)))
    assert replaced["open_time_ms"].to_list() == conflict_ts  # timestamps intactos


def test_replace_with_large_divergence_aborts_without_force(store: Path, incoming: Path) -> None:
    df = make_ohlcv([JAN_2024_MS + HOUR_MS], close_offset=20.0)  # ~20 %
    src = write_source(incoming, df, "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ConflictError, match="divergencia"):
        service.run_import(import_request(src, on_conflict="replace"))
    assert tree_fingerprint(store) == before


def test_replace_with_large_divergence_and_force(store: Path, incoming: Path) -> None:
    df = make_ohlcv([JAN_2024_MS + HOUR_MS], close_offset=20.0)
    plan = service.run_import(
        import_request(write_source(incoming, df, "parquet"), on_conflict="replace", force=True)
    ).import_plan
    assert plan is not None
    assert plan.max_close_divergence is not None
    assert plan.max_close_divergence > 0.01
    row = _existing_rows(store).filter(pl.col("open_time_ms") == JAN_2024_MS + HOUR_MS)
    assert row["close"].item() > 115


def test_replace_identical_values_is_unchanged(store: Path, incoming: Path) -> None:
    same = _existing_rows(store).head(3)
    src = write_source(incoming, same, "parquet")
    before = tree_fingerprint(store)
    result = service.run_import(import_request(src, on_conflict="replace"))
    assert tree_fingerprint(store) == before
    assert result.import_plan is not None
    assert result.import_plan.max_close_divergence == 0.0


def test_existing_rows_never_change_timestamp(store: Path, incoming: Path) -> None:
    before = _existing_rows(store)
    times = hours(JAN_2024_MS - 2 * HOUR_MS, 3)  # dic-2023 (nuevo), h0 existente
    src = write_source(incoming, make_ohlcv(times, close_offset=30.0), "parquet")
    service.run_import(import_request(src, on_conflict="skip"))
    after = _existing_rows(store)
    assert after.filter(pl.col("open_time_ms").is_in(before["open_time_ms"].implode())).equals(
        before
    )


def test_existing_multifile_partition_is_merged_into_data_parquet(
    parquet_dir: Path, incoming: Path
) -> None:
    jan = hours(JAN_2024_MS, 6)
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [make_ohlcv(jan[3:]), make_ohlcv(jan[:3])],
        names=["a.parquet", "b.parquet"],
    )
    src = write_source(incoming, make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "parquet")
    service.run_import(import_request(src))
    files = sorted(p.name for p in _root(parquet_dir).rglob("*.parquet"))
    assert files == ["data.parquet"]
    assert read_dataset(_root(parquet_dir)).height == 8


def test_existing_partition_with_other_schema_is_rejected(store: Path, incoming: Path) -> None:
    # el esquema del destino sale de sus archivos: un origen con el esquema canónico no encaja
    src = write_source(incoming, make_binance(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "parquet")
    with pytest.raises(ImportValidationError, match="sobran columnas"):
        service.run_import(import_request(src))


# --- T6.2: dos fases, dry-run, confirmación, concurrencia --------------------------------


def test_invalid_row_in_second_month_leaves_first_month_untouched(
    store: Path, incoming: Path
) -> None:
    good = hours(JAN_2024_MS + 10 * HOUR_MS, 2)
    bad = [FEB_2024_MS + 10 * HOUR_MS + 17]  # desalineada, en otra partición
    src = write_source(incoming, make_ohlcv([*good, *bad]), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError):
        service.run_import(import_request(src))
    assert tree_fingerprint(store) == before


def test_dry_run_returns_plan_and_writes_nothing(store: Path, incoming: Path) -> None:
    times = hours(JAN_2024_MS + 4 * HOUR_MS, 4)
    src = write_source(incoming, make_ohlcv([*times, FEB_2024_MS + 20 * HOUR_MS]), "parquet")
    before = tree_fingerprint(store)
    result = service.run_import(import_request(src, dry_run=True, confirm=False))
    assert tree_fingerprint(store) == before
    assert result.dry_run is True
    assert result.action == "import"
    plan = result.import_plan
    assert plan is not None
    assert (plan.rows_in_file, plan.rows_new, plan.rows_conflict) == (5, 3, 2)
    assert plan.partitions_modified == ["2024-01", "2024-02"]
    assert {r.result for r in result.partitions} == {"planned"}


def test_import_requires_confirmation_unless_dry_run(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ConfirmationRequiredError):
        service.run_import(import_request(src, confirm=False))
    assert tree_fingerprint(store) == before
    service.run_import(import_request(src, confirm=False, force=True))  # force basta


def test_partition_modified_concurrently_is_conflict_and_untouched(
    store: Path, incoming: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = write_source(incoming, make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "parquet")
    jan_dir = _root(store) / "year=2024" / "month=01"
    real_commit: Callable[..., str] = planner.commit

    def racing_commit(partition: planner.Partition, df: pl.DataFrame | None, base: Path) -> str:
        make_ohlcv([JAN_2024_MS + 99 * HOUR_MS]).write_parquet(jan_dir / "late.parquet")
        return real_commit(partition, df, base)

    before_dataset = read_dataset(_root(store))
    with monkeypatch.context() as m:
        m.setattr(planner, "commit", racing_commit)
        result = service.run_import(import_request(src))
    assert [r.result for r in result.partitions] == ["conflict"]
    assert sorted(p.name for p in jan_dir.iterdir()) == ["late.parquet", "part-000.parquet"]
    assert read_dataset(_root(store)).height == before_dataset.height + 1  # solo el archivo ajeno


def test_audit_event_per_partition(store: Path, incoming: Path) -> None:
    src = write_source(incoming, make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "parquet")
    with capture_logs() as logs:
        service.run_import(import_request(src))
    audits = [e for e in logs if e["event"] == "market_data_manager_audit"]
    assert [(e["action"], e["partition"], e["result"], e["dry_run"]) for e in audits] == [
        ("import", "2024-01", "changed", False)
    ]
    assert audits[0]["rows_before"] == 6
    assert audits[0]["rows_after"] == 8
    assert any(e["event"] == "market_data_manager_run" and e["action"] == "import" for e in logs)


def test_progress_callback_called_per_partition(parquet_dir: Path, incoming: Path) -> None:
    times = [FEB_2024_MS - HOUR_MS, FEB_2024_MS]
    src = write_source(incoming, make_binance(times), "parquet")
    seen: list[tuple[int, int, str]] = []
    service.run_import(
        import_request(src, create=True),
        lambda done, total, r: seen.append((done, total, r.result)),
    )
    assert seen == [(1, 2, "changed"), (2, 2, "changed")]


# --- T6.3 (servicio): ida y vuelta export -> import ---------------------------------------


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_export_then_import_round_trip_gives_identical_rows(
    parquet_dir: Path, incoming: Path, fmt: FullFormat
) -> None:
    times = [FEB_2024_MS - 2 * HOUR_MS, FEB_2024_MS - HOUR_MS, FEB_2024_MS, FEB_2024_MS + HOUR_MS]
    seed = make_binance(times, close_offset=0.123)
    write_partition_files(parquet_dir, 2024, 1, [seed.head(2)])
    write_partition_files(parquet_dir, 2024, 2, [seed.tail(2)])
    exported = incoming / f"export.{fmt}"
    files = sorted(_root(parquet_dir).rglob("*.parquet"))
    lazy = pl.scan_parquet(files).sort("open_time_ms")
    stream_export(lazy, fmt, exported)
    before = read_dataset(_root(parquet_dir))
    request = import_request(exported, symbol="ETHUSDT", create=True)
    plan = service.run_import(request).import_plan
    assert plan is not None
    assert plan.rows_new == 4
    imported = read_dataset(_root(parquet_dir, symbol="ETHUSDT"))
    assert imported.equals(before)
    assert imported.schema == before.schema


# --- Casos de borde de lectura -------------------------------------------------------------


def test_unreadable_source_is_a_validation_error(store: Path, incoming: Path) -> None:
    incoming.mkdir()
    path = incoming / "roto.parquet"
    path.write_bytes(b"esto no es parquet")
    before = tree_fingerprint(store)
    with pytest.raises(ImportValidationError, match="ilegible"):
        service.run_import(import_request(path))
    assert tree_fingerprint(store) == before


def test_non_numeric_price_column_in_parquet_is_rejected(store: Path, incoming: Path) -> None:
    df = make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)).with_columns(close=pl.Series(["a", "b"]))
    with pytest.raises(ImportValidationError, match="debe ser numérica"):
        service.run_import(import_request(write_source(incoming, df, "parquet")))


def test_non_integer_trades_column_in_parquet_is_rejected(store: Path, incoming: Path) -> None:
    df = make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)).with_columns(
        trades_count=pl.Series([1.5, 2.5])
    )
    with pytest.raises(ImportValidationError, match="debe ser entera"):
        service.run_import(import_request(write_source(incoming, df, "parquet")))


def test_existing_partition_with_mixed_schemas_is_rejected(
    parquet_dir: Path, incoming: Path
) -> None:
    jan = hours(JAN_2024_MS, 4)
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [
            make_ohlcv(jan[:2]),
            make_ohlcv(jan[2:]).with_columns(volume=pl.col("volume").cast(pl.Float64)),
        ],
        names=["a.parquet", "b.parquet"],
    )
    src = write_source(incoming, make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "parquet")
    before = tree_fingerprint(parquet_dir)
    with pytest.raises(ImportValidationError, match="existente"):
        service.run_import(import_request(src))
    assert tree_fingerprint(parquet_dir) == before


def test_io_error_on_one_partition_does_not_abort_the_others(
    parquet_dir: Path, incoming: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = [FEB_2024_MS - HOUR_MS, FEB_2024_MS]
    src = write_source(incoming, make_binance(times), "parquet")
    real_commit = planner.commit

    def flaky(partition: planner.Partition, df: pl.DataFrame | None, base: Path) -> str:
        if partition.month == 1:
            raise OSError("disco lleno")
        return real_commit(partition, df, base)

    with monkeypatch.context() as m:
        m.setattr(planner, "commit", flaky)
        result = service.run_import(import_request(src, create=True))
    assert [(r.month, r.result) for r in result.partitions] == [(1, "error"), (2, "changed")]
    assert "disco lleno" in (result.partitions[0].detail or "")


@pytest.mark.parametrize(
    "override",
    [{"symbol": "btcusdt"}, {"exchange": "Binance"}, {"symbol": "BtcUsdt", "create": True}],
)
def test_destination_with_different_capitalization_is_rejected(
    store: Path, incoming: Path, override: dict[str, object]
) -> None:
    """Un destino que solo difiere en mayúsculas del dataset existente no se acepta en silencio."""
    src = write_source(incoming, make_ohlcv(hourly()), "parquet")
    before = tree_fingerprint(store)
    with pytest.raises(InvalidArgumentError, match="capitalización"):
        service.run_import(import_request(src, **override))
    assert tree_fingerprint(store) == before
