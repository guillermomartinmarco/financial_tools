"""T4.1: CLI Typer de ft_market_data_manager sobre un almacén Parquet temporal."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import structlog
from finances_core.config import FinancesSettings
from typer.testing import CliRunner

from finances_downloader.ft_market_data_manager import cli, planner
from finances_downloader.ft_market_data_manager.cli import app
from finances_downloader.ft_market_data_manager.errors import (
    ConfirmationRequiredError,
    DatasetNotFoundError,
)

from .conftest import (
    FEB_2024_MS,
    JAN_2024_MS,
    dataset_root,
    hours,
    make_ohlcv,
    write_partition_files,
)

runner = CliRunner()
DS = ["-e", "binance", "-m", "spot", "-s", "BTCUSDT", "-t", "1h"]


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Cada invocación reconfigura logging hacia streams de CliRunner que luego se cierran."""
    yield
    logging.basicConfig(force=True, handlers=[logging.NullHandler()])
    structlog.reset_defaults()


@pytest.fixture
def store(parquet_dir: Path) -> Path:
    """Enero con dos archivos desordenados; febrero con duplicados. Siempre bajo tmp."""
    assert parquet_dir == FinancesSettings().PARQUET_DIR
    jan = hours(JAN_2024_MS, 6)
    write_partition_files(
        parquet_dir,
        2024,
        1,
        [make_ohlcv(jan[3:]), make_ohlcv(jan[:3])],
        names=["a.parquet", "b.parquet"],
    )
    feb = hours(FEB_2024_MS, 4)
    write_partition_files(
        parquet_dir, 2024, 2, [make_ohlcv(feb), make_ohlcv(feb[:2], close_offset=5.0)]
    )
    return parquet_dir


def _files(parquet_dir: Path) -> list[str]:
    return sorted(str(p.relative_to(parquet_dir)) for p in parquet_dir.rglob("*.parquet"))


def _json(stdout: str) -> dict[str, Any]:
    # CliRunner reproduce en stdout el eco de la entrada simulada (" y"): se descarta.
    data = json.loads(stdout[stdout.index("{") :])
    assert isinstance(data, dict)
    return data


def test_help_lists_commands() -> None:
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("compact", "dedupe", "prune"):
        assert cmd in res.stdout


def test_entry_point_is_registered() -> None:
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts["ft_market_data_manager"] == "finances_downloader.ft_market_data_manager.cli:app"


def test_compact_json_merges_files(store: Path) -> None:
    res = runner.invoke(app, ["compact", *DS, "-f", "json"])
    assert res.exit_code == 0, res.output
    body = _json(res.stdout)
    assert body["action"] == "compact"
    assert body["dry_run"] is False
    assert [p["result"] for p in body["partitions"]] == ["changed", "changed"]
    assert body["partitions"][0]["files_before"] == 2
    assert body["partitions"][0]["files_after"] == 1
    root = dataset_root(store)
    assert len(list((root / "year=2024" / "month=01").glob("*.parquet"))) == 1
    merged = pl.read_parquet(root / "year=2024" / "month=01" / "data.parquet")
    assert merged["open_time_ms"].to_list() == hours(JAN_2024_MS, 6)


def test_compact_dry_run_does_not_write(store: Path) -> None:
    before = _files(store)
    res = runner.invoke(app, ["compact", "--dry-run", "-f", "json"])
    assert res.exit_code == 0, res.output
    body = _json(res.stdout)
    assert body["dry_run"] is True
    assert {p["result"] for p in body["partitions"]} == {"planned"}
    assert _files(store) == before


def test_compact_table_output(store: Path) -> None:
    res = runner.invoke(app, ["compact", "--dry-run", *DS])
    assert res.exit_code == 0, res.output
    assert "compact (dry-run)" in res.stdout
    assert "planned" in res.stdout
    assert "2024-01" in res.stdout


def test_global_flags_before_command(store: Path) -> None:
    res = runner.invoke(
        app, ["--quiet", "--log-format", "json", "-f", "json", "dedupe", "--dry-run"]
    )
    assert res.exit_code == 0, res.output
    assert _json(res.stdout)["action"] == "dedupe"


def test_dedupe_removes_duplicates(store: Path) -> None:
    res = runner.invoke(app, ["dedupe", *DS, "-f", "json", "--start-ms", str(FEB_2024_MS)])
    assert res.exit_code == 0, res.output
    parts = _json(res.stdout)["partitions"]
    assert [(p["month"], p["result"], p["rows_before"], p["rows_after"]) for p in parts] == [
        (2, "changed", 6, 4)
    ]
    assert "duplicados eliminados" in parts[0]["detail"]


def test_dedupe_unchanged_exit_zero(store: Path) -> None:
    runner.invoke(app, ["dedupe", "-f", "json"])
    res = runner.invoke(app, ["dedupe", "-f", "json"])
    assert res.exit_code == 0
    assert {p["result"] for p in _json(res.stdout)["partitions"]} == {"unchanged"}


def test_prune_dry_run_lists_plan(store: Path) -> None:
    before = _files(store)
    res = runner.invoke(app, ["prune", "--all", "-e", "binance", "--dry-run", "-f", "json"])
    assert res.exit_code == 0, res.output
    body = _json(res.stdout)
    assert {p["result"] for p in body["partitions"]} == {"planned"}
    assert len(body["partitions"]) == 2
    assert _files(store) == before


def test_prune_force_deletes_only_complete_months_before_cutoff(store: Path) -> None:
    res = runner.invoke(
        app, ["prune", *DS, "--before-ms", str(FEB_2024_MS), "--force", "-f", "json"]
    )
    assert res.exit_code == 0, res.output
    assert [p["month"] for p in _json(res.stdout)["partitions"]] == [1]
    left = _files(store)
    assert left and all("month=02" in f for f in left)


def test_prune_rejected_confirmation_exits_1_and_deletes_nothing(store: Path) -> None:
    before = _files(store)
    res = runner.invoke(
        app, ["prune", *DS, "--before-ms", str(FEB_2024_MS), "-f", "json"], input="n\n"
    )
    assert res.exit_code == 1
    assert _files(store) == before


def test_prune_accepted_confirmation_deletes(store: Path) -> None:
    res = runner.invoke(
        app, ["prune", *DS, "--before-ms", str(FEB_2024_MS), "-f", "json"], input="y\n"
    )
    assert res.exit_code == 0, res.output
    assert _json(res.stdout)["partitions"][0]["result"] == "changed"
    assert all("month=02" in f for f in _files(store))


def test_prune_empty_plan_succeeds_without_prompt(store: Path) -> None:
    res = runner.invoke(app, ["prune", *DS, "--before-ms", str(JAN_2024_MS), "-f", "json"])
    assert res.exit_code == 0, res.output
    assert _json(res.stdout)["partitions"] == []


@pytest.mark.parametrize(
    "args",
    [
        ["prune", "--force"],
        ["prune", "--all", "--before-ms", "5", "--force"],
        ["prune", "--all", "--force"],
        ["compact", "--start-ms", "10", "--end-ms", "5"],
        ["dedupe", "--start-ms", "10", "--end-ms", "5"],
        ["compact", "--start-ms", "abc"],
    ],
)
def test_invalid_arguments_exit_2(store: Path, args: list[str]) -> None:
    before = _files(store)
    res = runner.invoke(app, args)
    assert res.exit_code == 2, res.output
    assert _files(store) == before


def test_partition_in_error_does_not_fail_the_command(store: Path) -> None:
    broken = dataset_root(store) / "year=2024" / "month=03"
    broken.mkdir(parents=True)
    (broken / "data.parquet").write_bytes(b"not parquet")
    res = runner.invoke(app, ["compact", *DS, "-f", "json"])
    assert res.exit_code == 0, res.output
    results = {p["month"]: p["result"] for p in _json(res.stdout)["partitions"]}
    assert results == {1: "changed", 2: "changed", 3: "error"}


def test_partition_in_conflict_exits_0(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _files(store)
    monkeypatch.setattr(planner, "commit", lambda *_a, **_k: "conflict")
    res = runner.invoke(app, ["compact", "-f", "json"])
    assert res.exit_code == 0, res.output
    assert {p["result"] for p in _json(res.stdout)["partitions"]} == {"conflict"}
    assert _files(store) == before


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (DatasetNotFoundError("no existe"), 1),
        (ConfirmationRequiredError("falta confirmación"), 3),
        (PermissionError("disco"), 4),
        (RuntimeError("inesperado"), 4),
    ],
)
def test_error_to_exit_code_mapping(
    store: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception, code: int
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise exc

    monkeypatch.setattr(cli.service, "run_compact", boom)
    res = runner.invoke(app, ["compact"])
    assert res.exit_code == code


def test_no_args_shows_help() -> None:
    res = runner.invoke(app, [])
    assert "compact" in res.output
