"""T6.3: comando ``import`` de la CLI sobre un almacén Parquet temporal (RF-5, RF-10)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import structlog
from finances_core.config import FinancesSettings
from finances_core.export import stream_export
from typer.testing import CliRunner

from finances_downloader.ft_market_data_manager.cli import app

from .conftest import (
    FEB_2024_MS,
    HOUR_MS,
    JAN_2024_MS,
    dataset_root,
    hours,
    make_ohlcv,
    write_partition_files,
)
from .import_helpers import make_binance, read_dataset, tree_fingerprint, write_source

runner = CliRunner()
DEST = ["-e", "binance", "-m", "spot", "-s", "BTCUSDT", "-t", "1h"]


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    logging.basicConfig(force=True, handlers=[logging.NullHandler()])
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _tmp_store_only(parquet_dir: Path, tmp_path: Path) -> None:
    assert parquet_dir == FinancesSettings().PARQUET_DIR
    assert parquet_dir.is_relative_to(tmp_path)


@pytest.fixture
def store(parquet_dir: Path) -> Path:
    write_partition_files(parquet_dir, 2024, 1, [make_ohlcv(hours(JAN_2024_MS, 6))])
    write_partition_files(parquet_dir, 2024, 2, [make_ohlcv(hours(FEB_2024_MS, 4))])
    return parquet_dir


@pytest.fixture
def new_rows(tmp_path: Path) -> Path:
    """Dos velas nuevas de enero en CSV (fuera del almacén)."""
    return write_source(
        tmp_path / "incoming", make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)), "csv"
    )


def _json(stdout: str) -> dict[str, Any]:
    data = json.loads(stdout[stdout.index("{") :])
    assert isinstance(data, dict)
    return data


def test_help_lists_import_with_its_options() -> None:
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "import" in res.output
    helped = runner.invoke(app, ["import", "--help"])
    for option in (
        "--source",
        "--exchange",
        "--market-type",
        "--symbol",
        "--timeframe",
        "--create",
        "--ts-column",
        "--ts-unit",
        "--on-conflict",
        "--skip-invalid",
        "--dry-run",
        "--force",
    ):
        assert option in helped.output, option


def test_dry_run_prints_the_plan_and_writes_nothing(store: Path, new_rows: Path) -> None:
    before = tree_fingerprint(store)
    res = runner.invoke(app, ["import", "--source", str(new_rows), *DEST, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "plan de importación" in res.stdout
    assert "binance/spot/BTCUSDT/1h" in res.stdout
    assert "2024-01" in res.stdout
    assert tree_fingerprint(store) == before


def test_dry_run_json_contains_the_import_plan(store: Path, new_rows: Path) -> None:
    res = runner.invoke(
        app, ["-f", "json", "import", "--source", str(new_rows), *DEST, "--dry-run"]
    )
    assert res.exit_code == 0, res.output
    body = _json(res.stdout)
    assert body["action"] == "import"
    assert body["dry_run"] is True
    plan = body["import_plan"]
    assert (plan["rows_in_file"], plan["rows_new"], plan["rows_conflict"]) == (2, 2, 0)
    assert plan["partitions_modified"] == ["2024-01"]
    assert body["partitions"][0]["result"] == "planned"


def test_output_option_selects_json_for_the_command(store: Path, new_rows: Path) -> None:
    res = runner.invoke(
        app, ["import", "--source", str(new_rows), *DEST, "--dry-run", "--output", "json"]
    )
    assert res.exit_code == 0, res.output
    assert _json(res.stdout)["import_plan"]["rows_new"] == 2


def test_force_shows_the_preview_and_imports_without_prompt(store: Path, new_rows: Path) -> None:
    res = runner.invoke(app, ["import", "--source", str(new_rows), *DEST, "--force", "-o", "json"])
    assert res.exit_code == 0, res.output
    assert "vista previa de la importación" in res.output
    assert "¿Confirma" not in res.output
    assert _json(res.stdout)["partitions"][0]["result"] == "changed"
    assert read_dataset(dataset_root(store)).height == 12


def test_rejected_confirmation_exits_1_and_writes_nothing(store: Path, new_rows: Path) -> None:
    before = tree_fingerprint(store)
    res = runner.invoke(app, ["import", "--source", str(new_rows), *DEST], input="n\n")
    assert res.exit_code == 1
    assert "vista previa de la importación" in res.output
    assert "¿Confirma la importación?" in res.output
    assert tree_fingerprint(store) == before


def test_accepted_confirmation_imports(store: Path, new_rows: Path) -> None:
    res = runner.invoke(app, ["import", "--source", str(new_rows), *DEST], input="y\n")
    assert res.exit_code == 0, res.output
    assert "vista previa de la importación" in res.output
    assert read_dataset(dataset_root(store)).height == 12


def test_missing_source_exits_1(store: Path, tmp_path: Path) -> None:
    res = runner.invoke(app, ["import", "--source", str(tmp_path / "x.csv"), *DEST, "--force"])
    assert res.exit_code == 1
    assert "origen" in res.output


def test_missing_destination_exits_1_and_create_makes_it(parquet_dir: Path, tmp_path: Path) -> None:
    src = write_source(tmp_path / "incoming", make_binance(hours(JAN_2024_MS, 3)), "parquet")
    res = runner.invoke(app, ["import", "--source", str(src), *DEST, "--force"])
    assert res.exit_code == 1
    assert "--create" in res.output
    assert list(parquet_dir.iterdir()) == []
    ok = runner.invoke(app, ["import", "--source", str(src), *DEST, "--force", "--create"])
    assert ok.exit_code == 0, ok.output
    assert read_dataset(dataset_root(parquet_dir)).height == 3


def test_missing_required_destination_option_exits_2(store: Path, new_rows: Path) -> None:
    res = runner.invoke(app, ["import", "--source", str(new_rows), "-e", "binance", "--dry-run"])
    assert res.exit_code == 2


def test_missing_columns_exit_2(store: Path, tmp_path: Path) -> None:
    src = write_source(
        tmp_path / "incoming", make_ohlcv(hours(JAN_2024_MS, 2)).drop("close"), "csv"
    )
    before = tree_fingerprint(store)
    res = runner.invoke(app, ["import", "--source", str(src), *DEST, "--force"])
    assert res.exit_code == 2
    assert "close" in res.output
    assert tree_fingerprint(store) == before


def test_seconds_instead_of_milliseconds_exit_2_with_hint(store: Path, tmp_path: Path) -> None:
    src = write_source(
        tmp_path / "incoming", make_ohlcv([t // 1000 for t in hours(JAN_2024_MS, 2)]), "csv"
    )
    res = runner.invoke(app, ["import", "--source", str(src), *DEST, "--dry-run"])
    assert res.exit_code == 2
    assert "--ts-unit s" in res.output


def test_ts_column_and_unit_options(store: Path, tmp_path: Path) -> None:
    times = hours(JAN_2024_MS + 10 * HOUR_MS, 2)
    df = make_ohlcv([t // 1000 for t in times]).rename({"open_time_ms": "ts"})
    src = write_source(tmp_path / "incoming", df, "csv")
    res = runner.invoke(
        app,
        ["import", "--source", str(src), *DEST, "--ts-column", "ts", "--ts-unit", "s", "--force"],
    )
    assert res.exit_code == 0, res.output
    assert times[0] in read_dataset(dataset_root(store))["open_time_ms"].to_list()


def test_file_format_option_overrides_extension(store: Path, tmp_path: Path) -> None:
    (tmp_path / "incoming").mkdir()
    path = tmp_path / "incoming" / "velas.dat"
    make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)).write_csv(path)
    bad = runner.invoke(app, ["import", "--source", str(path), *DEST, "--dry-run"])
    assert bad.exit_code == 2
    ok = runner.invoke(
        app, ["import", "--source", str(path), *DEST, "--dry-run", "--format", "csv"]
    )
    assert ok.exit_code == 0, ok.output


def test_on_conflict_fail_exits_3(store: Path, tmp_path: Path) -> None:
    src = write_source(tmp_path / "incoming", make_ohlcv(hours(JAN_2024_MS, 2)), "parquet")
    before = tree_fingerprint(store)
    res = runner.invoke(
        app, ["import", "--source", str(src), *DEST, "--on-conflict", "fail", "--force"]
    )
    assert res.exit_code == 3
    assert tree_fingerprint(store) == before


def test_replace_divergence_exits_3_unless_force(store: Path, tmp_path: Path) -> None:
    src = write_source(
        tmp_path / "incoming", make_ohlcv([JAN_2024_MS + HOUR_MS], close_offset=20.0), "parquet"
    )
    args = ["import", "--source", str(src), *DEST, "--on-conflict", "replace"]
    before = tree_fingerprint(store)
    refused = runner.invoke(app, [*args, "--dry-run"])
    assert refused.exit_code == 3
    assert "divergencia" in refused.output
    assert tree_fingerprint(store) == before
    forced = runner.invoke(app, [*args, "--force"])
    assert forced.exit_code == 0, forced.output
    assert "divergencia máx. de close" in forced.output


def test_skip_invalid_reports_rejected_rows(store: Path, tmp_path: Path) -> None:
    df = make_ohlcv(hours(JAN_2024_MS + 10 * HOUR_MS, 2)).with_columns(
        close=pl.Series([100.0, 0.0], dtype=pl.Float32)
    )
    src = write_source(tmp_path / "incoming", df, "csv")
    refused = runner.invoke(app, ["import", "--source", str(src), *DEST, "--dry-run"])
    assert refused.exit_code == 2
    ok = runner.invoke(
        app, ["-f", "json", "import", "--source", str(src), *DEST, "--skip-invalid", "--force"]
    )
    assert ok.exit_code == 0, ok.output
    plan = _json(ok.stdout)["import_plan"]
    assert (plan["rows_new"], plan["rows_rejected"]) == (1, 1)


def test_source_inside_the_store_exits_2(store: Path) -> None:
    inside = store / "binance" / "x.csv"
    make_ohlcv(hours(JAN_2024_MS, 2)).write_csv(inside)
    res = runner.invoke(app, ["import", "--source", str(inside), *DEST, "--dry-run"])
    assert res.exit_code == 2
    assert "FT_PARQUET_DIR" in res.output


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_round_trip_export_then_import_through_the_cli(
    parquet_dir: Path, tmp_path: Path, fmt: str
) -> None:
    seed = make_binance(
        [FEB_2024_MS - HOUR_MS, FEB_2024_MS, FEB_2024_MS + HOUR_MS], close_offset=0.321
    )
    write_partition_files(parquet_dir, 2024, 1, [seed.head(1)])
    write_partition_files(parquet_dir, 2024, 2, [seed.tail(2)])
    source = read_dataset(dataset_root(parquet_dir))
    exported = tmp_path / "out" / f"btc.{fmt}"
    stream_export(source.lazy(), fmt, exported)  # type: ignore[arg-type]
    res = runner.invoke(
        app,
        ["import", "--source", str(exported), "-e", "binance", "-m", "spot", "-s", "ETHUSDT"]
        + ["-t", "1h", "--create", "--force"],
    )
    assert res.exit_code == 0, res.output
    imported = read_dataset(dataset_root(parquet_dir, symbol="ETHUSDT"))
    assert imported.equals(source)
    assert imported.schema == source.schema
