"""T2.1, T2.2, T3.1: compact / dedupe / prune sobre un almacén Parquet real temporal."""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas import ManagerRequest, ManagerResult, PruneRequest
from structlog.testing import capture_logs

from finances_downloader.ft_market_data_manager import planner, service
from finances_downloader.ft_market_data_manager.errors import (
    ConfirmationRequiredError,
    InvalidArgumentError,
)

from .conftest import (
    FEB_2024_MS,
    HOUR_MS,
    JAN_2024_MS,
    MAR_2024_MS,
    dataset_root,
    hours,
    make_ohlcv,
    write_partition_files,
)

AUDIT_FIELDS = {
    "action",
    "dataset",
    "partition",
    "rows_before",
    "rows_after",
    "bytes_before",
    "bytes_after",
    "dry_run",
    "result",
}


def _month_dir(parquet_dir: Path, year: int, month: int, **ds: str) -> Path:
    return dataset_root(parquet_dir, **ds) / f"year={year:04d}" / f"month={month:02d}"


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _mtimes(root: Path) -> dict[str, int]:
    return {str(p): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}


def _scrambled_partition(parquet_dir: Path, **ds: str) -> list[Path]:
    """Dos archivos desordenados entre sí: (h4,h5) y (h0,h1,h2)."""
    return write_partition_files(
        parquet_dir,
        2024,
        1,
        [
            make_ohlcv([JAN_2024_MS + 4 * HOUR_MS, JAN_2024_MS + 5 * HOUR_MS]),
            make_ohlcv(hours(JAN_2024_MS, 3)),
        ],
        **ds,
    )


# --- T2.1 compact ------------------------------------------------------------------------


def test_compact_merges_files_sorted_without_losing_rows(parquet_dir: Path) -> None:
    files = _scrambled_partition(parquet_dir)
    expected = pl.concat([pl.read_parquet(f) for f in files]).sort("open_time_ms")
    size_before = sum(f.stat().st_size for f in files)
    result = service.run_compact(ManagerRequest())
    assert isinstance(result, ManagerResult)
    assert result.action == "compact"
    (part,) = result.partitions
    assert (part.result, part.files_before, part.files_after) == ("changed", 2, 1)
    assert (part.rows_before, part.rows_after) == (5, 5)
    assert part.bytes_before == size_before
    month = _month_dir(parquet_dir, 2024, 1)
    assert _names(month) == ["data.parquet"]
    assert part.bytes_after == (month / "data.parquet").stat().st_size
    out = pl.read_parquet(month / "data.parquet")
    assert out.equals(expected)
    assert out["open_time_ms"].is_sorted()


def test_compact_keeps_duplicates_and_row_order_is_stable(parquet_dir: Path) -> None:
    t = JAN_2024_MS
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [make_ohlcv([t + HOUR_MS, t], close_offset=1.0), make_ohlcv([t], close_offset=2.0)],
    )
    service.run_compact(ManagerRequest())
    out = pl.read_parquet(_month_dir(parquet_dir, 2024, 1) / "data.parquet")
    assert out["open_time_ms"].to_list() == [t, t, t + HOUR_MS]
    # sort estable: entre filas con igual tiempo se conserva el orden de lectura
    closes = out.filter(pl.col("open_time_ms") == t)["close"].to_list()
    assert closes[0] < closes[1]


def test_compact_preserves_dtypes(parquet_dir: Path) -> None:
    _scrambled_partition(parquet_dir)
    service.run_compact(ManagerRequest())
    out = pl.read_parquet(_month_dir(parquet_dir, 2024, 1) / "data.parquet")
    assert dict(out.schema) == {
        "open_time_ms": pl.Int64,
        "open": pl.Float32,
        "high": pl.Float32,
        "low": pl.Float32,
        "close": pl.Float32,
        "volume": pl.Float32,
        "trades_count": pl.Int64,
    }


def test_compact_is_idempotent_and_does_not_touch_mtime(parquet_dir: Path) -> None:
    _scrambled_partition(parquet_dir)
    service.run_compact(ManagerRequest())
    target = _month_dir(parquet_dir, 2024, 1) / "data.parquet"
    before = target.stat().st_mtime_ns
    second = service.run_compact(ManagerRequest())
    assert [p.result for p in second.partitions] == ["unchanged"]
    assert second.partitions[0].files_after == 1
    assert target.stat().st_mtime_ns == before


def test_compact_unchanged_for_single_sorted_file_with_any_name(parquet_dir: Path) -> None:
    (path,) = write_partition_files(
        parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))], names=["only.parquet"]
    )
    before = path.stat().st_mtime_ns
    result = service.run_compact(ManagerRequest())
    assert [p.result for p in result.partitions] == ["unchanged"]
    assert path.stat().st_mtime_ns == before


def test_compact_sorts_single_unsorted_file(parquet_dir: Path) -> None:
    t = JAN_2024_MS
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t + HOUR_MS, t])])
    (part,) = service.run_compact(ManagerRequest()).partitions
    assert (part.result, part.files_before, part.files_after) == ("changed", 1, 1)
    out = pl.read_parquet(_month_dir(parquet_dir, 2024, 1) / "data.parquet")
    assert out["open_time_ms"].to_list() == [t, t + HOUR_MS]


def test_compact_dry_run_writes_nothing(parquet_dir: Path) -> None:
    _scrambled_partition(parquet_dir)
    root = parquet_dir
    before_files = _mtimes(root)
    result = service.run_compact(ManagerRequest(dry_run=True))
    assert result.dry_run is True
    (part,) = result.partitions
    assert (part.result, part.files_before, part.files_after) == ("planned", 2, 1)
    assert part.rows_after == 5
    assert part.bytes_after > 0
    assert _mtimes(root) == before_files
    assert not (_month_dir(parquet_dir, 2024, 1) / "data.parquet").exists()


def test_compact_selector_limits_scope(parquet_dir: Path) -> None:
    _scrambled_partition(parquet_dir)
    _scrambled_partition(parquet_dir, symbol="ETHUSDT")
    result = service.run_compact(ManagerRequest(symbol=["ETHUSDT"]))
    assert [p.dataset for p in result.partitions] == ["binance/spot/ETHUSDT/1h"]
    assert len(_names(_month_dir(parquet_dir, 2024, 1))) == 2  # BTC intacto
    assert _names(_month_dir(parquet_dir, 2024, 1, symbol="ETHUSDT")) == ["data.parquet"]


def test_compact_no_matching_partitions_is_empty_success(parquet_dir: Path) -> None:
    result = service.run_compact(ManagerRequest(symbol=["NOPE"]))
    assert result.partitions == []


def test_compact_one_broken_partition_does_not_abort_others(parquet_dir: Path) -> None:
    _scrambled_partition(parquet_dir, symbol="AAAUSDT")
    bad = write_partition_files(
        parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 2))], symbol="BBBUSDT"
    )
    bad[0].write_bytes(b"this is not parquet")
    _scrambled_partition(parquet_dir, symbol="CCCUSDT")
    bad_bytes = bad[0].read_bytes()

    result = service.run_compact(ManagerRequest())
    by_ds = {p.dataset: p for p in result.partitions}
    assert by_ds["binance/spot/AAAUSDT/1h"].result == "changed"
    assert by_ds["binance/spot/CCCUSDT/1h"].result == "changed"
    broken = by_ds["binance/spot/BBBUSDT/1h"]
    assert broken.result == "error"
    assert broken.detail
    assert bad[0].read_bytes() == bad_bytes
    assert _names(_month_dir(parquet_dir, 2024, 1, symbol="BBBUSDT")) == [bad[0].name]


def test_compact_partition_breaking_canonical_shape_is_error_and_untouched(
    parquet_dir: Path,
) -> None:
    ok = make_ohlcv(hours(JAN_2024_MS, 2))
    other = ok.with_columns(pl.col("volume").cast(pl.Float64))  # esquema distinto entre archivos
    files = write_partition_files(parquet_dir, 2024, 1, [ok, other])
    (part,) = service.run_compact(ManagerRequest()).partitions
    assert part.result == "error"
    assert all(f.exists() for f in files)
    assert not (files[0].parent / "data.parquet").exists()


def test_compact_partition_without_open_time_column_is_error(parquet_dir: Path) -> None:
    frame = make_ohlcv(hours(JAN_2024_MS, 2)).rename({"open_time_ms": "ts"})
    (path,) = write_partition_files(parquet_dir, 2024, 1, [frame])
    (part,) = service.run_compact(ManagerRequest()).partitions
    assert part.result == "error"
    assert "open_time_ms" in (part.detail or "")
    assert path.exists()


def test_compact_partition_with_null_open_time_is_error(parquet_dir: Path) -> None:
    frame = make_ohlcv(hours(JAN_2024_MS, 2)).with_columns(
        pl.when(pl.col("open_time_ms") == JAN_2024_MS)
        .then(None)
        .otherwise(pl.col("open_time_ms"))
        .alias("open_time_ms")
    )
    write_partition_files(parquet_dir, 2024, 1, [frame])
    (part,) = service.run_compact(ManagerRequest()).partitions
    assert part.result == "error"


def test_compact_conflict_when_partition_changes_between_plan_and_commit(
    parquet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _scrambled_partition(parquet_dir)
    real_commit = planner.commit

    def racing_commit(
        partition: planner.Partition, df: pl.DataFrame | None, base: Path
    ) -> planner.CommitResult:
        make_ohlcv([JAN_2024_MS + 50 * HOUR_MS]).write_parquet(files[0])  # escritura ajena
        return real_commit(partition, df, base)

    monkeypatch.setattr(planner, "commit", racing_commit)
    (part,) = service.run_compact(ManagerRequest()).partitions
    assert part.result == "conflict"
    assert part.files_after == part.files_before == 2
    assert _names(_month_dir(parquet_dir, 2024, 1)) == sorted(f.name for f in files)
    assert pl.read_parquet(files[0])["open_time_ms"].to_list() == [JAN_2024_MS + 50 * HOUR_MS]


def test_compact_emits_audit_events(parquet_dir: Path) -> None:
    _scrambled_partition(parquet_dir)
    with capture_logs() as logs:
        service.run_compact(ManagerRequest())
    audits = [e for e in logs if e.get("event") == "market_data_manager_audit"]
    assert len(audits) == 1
    event = audits[0]
    assert event.keys() >= AUDIT_FIELDS
    assert event["log_level"] == "info"
    assert event["component"] == "ft_market_data_manager"
    assert event["action"] == "compact"
    assert event["dataset"] == "binance/spot/BTCUSDT/1h"
    assert event["partition"] == "2024-01"
    assert (event["rows_before"], event["rows_after"]) == (5, 5)
    assert event["result"] == "changed"
    assert event["dry_run"] is False
    summary = [e for e in logs if e.get("event") == "market_data_manager_run"]
    assert summary
    assert {"duration_ms", "partitions_scanned", "rows_processed"} <= summary[0].keys()


def test_compact_audit_events_report_dry_run(parquet_dir: Path) -> None:
    _scrambled_partition(parquet_dir)
    with capture_logs() as logs:
        service.run_compact(ManagerRequest(dry_run=True))
    (event,) = [e for e in logs if e.get("event") == "market_data_manager_audit"]
    assert (event["dry_run"], event["result"]) == (True, "planned")


def test_settings_are_read_per_call(
    parquet_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scrambled_partition(parquet_dir)
    other_root = tmp_path / "other"
    monkeypatch.setenv("FT_PROJECT_ROOT", str(other_root))
    assert service.run_compact(ManagerRequest()).partitions == []  # almacén vacío distinto
    monkeypatch.setenv("FT_PROJECT_ROOT", str(tmp_path))
    assert len(service.run_compact(ManagerRequest()).partitions) == 1


# --- T2.2 dedupe -------------------------------------------------------------------------


def test_dedupe_keeps_last_occurrence_in_read_order(parquet_dir: Path) -> None:
    t = JAN_2024_MS
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [
            make_ohlcv([t, t + HOUR_MS, t + 2 * HOUR_MS], close_offset=0.0),
            make_ohlcv([t + HOUR_MS, t + 3 * HOUR_MS], close_offset=10.0),
            make_ohlcv([t + HOUR_MS], close_offset=20.0),  # la última en orden de lectura
        ],
    )
    result = service.run_dedupe(ManagerRequest())
    assert result.action == "dedupe"
    (part,) = result.partitions
    assert (part.result, part.rows_before, part.rows_after) == ("changed", 6, 4)
    assert (part.files_before, part.files_after) == (3, 1)
    assert "2" in (part.detail or "")  # duplicados eliminados
    assert str(t + HOUR_MS) in (part.detail or "")  # ejemplo de open_time_ms
    out = pl.read_parquet(_month_dir(parquet_dir, 2024, 1) / "data.parquet")
    assert out["open_time_ms"].to_list() == [t + i * HOUR_MS for i in range(4)]
    kept = out.filter(pl.col("open_time_ms") == t + HOUR_MS)["close"].item()
    assert kept == pytest.approx(120.0 + ((t + HOUR_MS) % 7) / 10, rel=1e-5)
    assert _names(_month_dir(parquet_dir, 2024, 1)) == ["data.parquet"]


def test_dedupe_within_a_single_file(parquet_dir: Path) -> None:
    t = JAN_2024_MS
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t + HOUR_MS, t, t + HOUR_MS])])
    (part,) = service.run_dedupe(ManagerRequest()).partitions
    assert (part.result, part.rows_before, part.rows_after) == ("changed", 3, 2)
    out = pl.read_parquet(_month_dir(parquet_dir, 2024, 1) / "data.parquet")
    assert out["open_time_ms"].to_list() == [t, t + HOUR_MS]


def test_dedupe_without_duplicates_is_unchanged_and_untouched(parquet_dir: Path) -> None:
    files = _scrambled_partition(parquet_dir)
    before = {f: f.stat().st_mtime_ns for f in files}
    (part,) = service.run_dedupe(ManagerRequest()).partitions
    assert part.result == "unchanged"
    assert {f: f.stat().st_mtime_ns for f in files} == before
    assert _names(_month_dir(parquet_dir, 2024, 1)) == sorted(f.name for f in files)


def test_dedupe_preserves_dtypes_and_is_idempotent(parquet_dir: Path) -> None:
    t = JAN_2024_MS
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t, t]), make_ohlcv([t + HOUR_MS])])
    service.run_dedupe(ManagerRequest())
    target = _month_dir(parquet_dir, 2024, 1) / "data.parquet"
    out = pl.read_parquet(target)
    assert out.schema["open"] == pl.Float32
    assert out.schema["close"] == pl.Float32
    assert out.schema["open_time_ms"] == pl.Int64
    assert out.schema["trades_count"] == pl.Int64
    mtime = target.stat().st_mtime_ns
    second = service.run_dedupe(ManagerRequest())
    assert [p.result for p in second.partitions] == ["unchanged"]
    assert target.stat().st_mtime_ns == mtime


def test_dedupe_dry_run_writes_nothing(parquet_dir: Path) -> None:
    t = JAN_2024_MS
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t, t]), make_ohlcv([t + HOUR_MS])])
    before = _mtimes(parquet_dir)
    (part,) = service.run_dedupe(ManagerRequest(dry_run=True)).partitions
    assert (part.result, part.rows_before, part.rows_after) == ("planned", 3, 2)
    assert _mtimes(parquet_dir) == before
    assert not (_month_dir(parquet_dir, 2024, 1) / "data.parquet").exists()


def test_dedupe_conflict_and_error_isolation(
    parquet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = JAN_2024_MS
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t, t])], symbol="AAAUSDT")
    bad = write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t])], symbol="BBBUSDT")
    bad[0].write_bytes(b"garbage")
    raced = write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t, t])], symbol="CCCUSDT")
    real_commit = planner.commit

    def racing(
        partition: planner.Partition, df: pl.DataFrame | None, base: Path
    ) -> planner.CommitResult:
        if partition.label.endswith("CCCUSDT/1h"):
            os.utime(raced[0], ns=(1, 1))
        return real_commit(partition, df, base)

    monkeypatch.setattr(planner, "commit", racing)
    by_ds = {
        p.dataset.split("/")[2]: p.result for p in service.run_dedupe(ManagerRequest()).partitions
    }
    assert by_ds == {"AAAUSDT": "changed", "BBBUSDT": "error", "CCCUSDT": "conflict"}


def test_dedupe_emits_audit_with_duplicate_detail(parquet_dir: Path) -> None:
    t = JAN_2024_MS
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv([t, t])])
    with capture_logs() as logs:
        service.run_dedupe(ManagerRequest())
    (event,) = [e for e in logs if e.get("event") == "market_data_manager_audit"]
    assert event.keys() >= AUDIT_FIELDS
    assert (event["action"], event["result"], event["rows_before"], event["rows_after"]) == (
        "dedupe",
        "changed",
        2,
        1,
    )


# --- T3.1 prune --------------------------------------------------------------------------


def _three_months(parquet_dir: Path, **ds: str) -> None:
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))], **ds)
    write_partition_files(parquet_dir, 2024, 2, [make_ohlcv(hours(FEB_2024_MS, 3))], **ds)
    write_partition_files(parquet_dir, 2024, 3, [make_ohlcv(hours(MAR_2024_MS, 3))], **ds)


def test_prune_before_ms_deletes_only_complete_earlier_months(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    cutoff = FEB_2024_MS + 5 * HOUR_MS  # dentro de febrero
    result = service.run_prune(PruneRequest(before_ms=cutoff, confirm=True))
    assert result.action == "prune"
    assert [(p.year, p.month, p.result) for p in result.partitions] == [(2024, 1, "changed")]
    assert not _month_dir(parquet_dir, 2024, 1).exists()
    assert _month_dir(parquet_dir, 2024, 2).exists()  # contiene T: intacto
    assert _month_dir(parquet_dir, 2024, 3).exists()
    (part,) = result.partitions
    assert (part.rows_before, part.rows_after, part.files_after, part.bytes_after) == (3, 0, 0, 0)


def test_prune_cutoff_on_month_boundary_deletes_previous_month(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    result = service.run_prune(PruneRequest(before_ms=FEB_2024_MS, confirm=True))
    assert [(p.year, p.month) for p in result.partitions] == [(2024, 1)]
    one_less = service.run_prune(PruneRequest(before_ms=MAR_2024_MS - 1, confirm=True))
    assert one_less.partitions == []  # febrero termina en MAR_2024_MS > T
    assert _month_dir(parquet_dir, 2024, 2).exists()


def test_prune_removes_empty_hive_dirs(parquet_dir: Path) -> None:
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    write_partition_files(
        parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))], symbol="ETHUSDT"
    )
    service.run_prune(PruneRequest(all=True, symbol=["BTCUSDT"], confirm=True))
    assert not dataset_root(parquet_dir).exists()
    assert not (parquet_dir / "binance" / "market_type=spot" / "symbol=BTCUSDT").exists()
    assert dataset_root(parquet_dir, symbol="ETHUSDT").exists()


def test_prune_all_deletes_every_partition_of_selected_dataset_only(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    _three_months(parquet_dir, symbol="ETHUSDT")
    result = service.run_prune(PruneRequest(all=True, symbol=["BTCUSDT"], confirm=True))
    assert len(result.partitions) == 3
    assert {p.result for p in result.partitions} == {"changed"}
    assert not any(parquet_dir.rglob("symbol=BTCUSDT"))
    assert len(list(parquet_dir.rglob("*.parquet"))) == 3


def test_prune_without_confirm_and_without_dry_run_deletes_nothing(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    before = _mtimes(parquet_dir)
    with pytest.raises(ConfirmationRequiredError):
        service.run_prune(PruneRequest(before_ms=MAR_2024_MS))
    with pytest.raises(ConfirmationRequiredError):
        service.run_prune(PruneRequest(all=True, symbol=["BTCUSDT"]))
    assert _mtimes(parquet_dir) == before


def test_prune_dry_run_returns_plan_without_deleting(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    before = _mtimes(parquet_dir)
    result = service.run_prune(PruneRequest(before_ms=MAR_2024_MS, dry_run=True))
    assert result.dry_run is True
    assert [(p.month, p.result) for p in result.partitions] == [(1, "planned"), (2, "planned")]
    assert all(p.rows_before == 3 and p.rows_after == 0 for p in result.partitions)
    assert _mtimes(parquet_dir) == before


def test_prune_dry_run_does_not_require_confirmation_even_with_confirm_false(
    parquet_dir: Path,
) -> None:
    _three_months(parquet_dir)
    result = service.run_prune(PruneRequest(all=True, exchange=["binance"], dry_run=True))
    assert len(result.partitions) == 3


def test_prune_nothing_matching_is_empty_success(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    assert service.run_prune(PruneRequest(before_ms=JAN_2024_MS, confirm=True)).partitions == []


def test_prune_respects_selector_range(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    result = service.run_prune(
        PruneRequest(before_ms=MAR_2024_MS + HOUR_MS, start_ms=FEB_2024_MS, confirm=True)
    )
    assert [(p.year, p.month) for p in result.partitions] == [(2024, 2)]
    assert _month_dir(parquet_dir, 2024, 1).exists()


def test_prune_conflict_leaves_partition_and_continues(
    parquet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three_months(parquet_dir)
    jan_file = next(_month_dir(parquet_dir, 2024, 1).glob("*.parquet"))
    real_commit = planner.commit

    def racing(
        partition: planner.Partition, df: pl.DataFrame | None, base: Path
    ) -> planner.CommitResult:
        if (partition.year, partition.month) == (2024, 1):
            os.utime(jan_file, ns=(1, 1))
        return real_commit(partition, df, base)

    monkeypatch.setattr(planner, "commit", racing)
    result = service.run_prune(PruneRequest(before_ms=MAR_2024_MS + 1, confirm=True))
    assert [(p.month, p.result) for p in result.partitions] == [(1, "conflict"), (2, "changed")]
    assert jan_file.exists()
    assert not _month_dir(parquet_dir, 2024, 2).exists()


def test_prune_unreadable_partition_is_still_deleted(parquet_dir: Path) -> None:
    (path,) = write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 3))])
    path.write_bytes(b"garbage")
    (part,) = service.run_prune(PruneRequest(before_ms=FEB_2024_MS, confirm=True)).partitions
    assert part.result == "changed"
    assert not path.exists()


def test_prune_emits_audit_events(parquet_dir: Path) -> None:
    _three_months(parquet_dir)
    with capture_logs() as logs:
        service.run_prune(PruneRequest(before_ms=FEB_2024_MS, confirm=True))
        service.run_prune(PruneRequest(before_ms=MAR_2024_MS, dry_run=True))
    audits = [e for e in logs if e.get("event") == "market_data_manager_audit"]
    assert [(e["action"], e["result"], e["dry_run"]) for e in audits] == [
        ("prune", "changed", False),
        ("prune", "planned", True),
    ]
    assert all(e.keys() >= AUDIT_FIELDS for e in audits)
    assert audits[0]["rows_before"] == 3
    assert audits[0]["rows_after"] == 0


def test_compact_leftover_cleanup_failure_is_reported_and_dedupe_heals(
    parquet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _scrambled_partition(parquet_dir)
    locked = files[1]
    real_unlink = Path.unlink

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == locked:
            msg = "file in use"
            raise PermissionError(msg)
        real_unlink(self, missing_ok=missing_ok)

    # context(): no usar monkeypatch.undo(), que también desharía FT_PARQUET_DIR y apuntaría al
    # almacén real.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", flaky_unlink)
        (part,) = service.run_compact(ManagerRequest()).partitions

    month = _month_dir(parquet_dir, 2024, 1)
    assert part.result == "error"
    assert len(_names(month)) == 2
    assert part.files_after == 2  # estado real en disco, no el anterior
    assert locked.name in (part.detail or "")
    assert "dedupe" in (part.detail or "")

    assert parquet_dir == FinancesSettings().PARQUET_DIR  # nunca operar sobre el almacén real
    (healed,) = service.run_dedupe(ManagerRequest()).partitions
    assert healed.result == "changed"
    assert _names(month) == ["data.parquet"]


def test_progress_callback_reports_every_partition(parquet_dir: Path) -> None:
    """El gancho opcional recibe ``(hechas, total, resultado)`` sin alterar el resultado."""
    for month in (1, 2):
        start = JAN_2024_MS if month == 1 else FEB_2024_MS
        write_partition_files(parquet_dir, 2024, month, [make_ohlcv(hours(start, 3))])
    calls: list[tuple[int, int, str]] = []

    def record(done: int, total: int, result: object) -> None:
        calls.append((done, total, getattr(result, "result", "")))

    plain = service.run_compact(ManagerRequest(dry_run=True))
    with_hook = service.run_compact(ManagerRequest(dry_run=True), record)
    assert with_hook == plain
    service.run_dedupe(ManagerRequest(dry_run=True), record)
    service.run_prune(PruneRequest(all=True, exchange=["binance"], dry_run=True), record)
    assert [c[:2] for c in calls] == [(1, 2), (2, 2)] * 3


def test_validate_selector_rejects_inverted_range() -> None:
    service.validate_selector(ManagerRequest(start_ms=1, end_ms=1))
    service.validate_selector(ManagerRequest(start_ms=5))
    with pytest.raises(InvalidArgumentError):
        service.validate_selector(ManagerRequest(start_ms=2, end_ms=1))
