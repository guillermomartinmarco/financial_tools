"""T5.3: CLI Typer — comandos, flags globales y exit codes RF-11 (0/1/2/3/4)."""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import IndicatorComputeRequest, IndicatorSpec
from finances_core.schemas.market_data import TF_MS_MAP
from typer.testing import CliRunner

from finances_analytics.ft_indicators import cli, service

from .conftest import JAN_2024_MS, DatasetFactory

runner = CliRunner()
HOUR = TF_MS_MAP["1h"]
UNKNOWN = "0192f000-0000-7000-8000-0000000000ff"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DS = "binance/spot/BTCUSDT/1h"


def task_id_from(output: str) -> str:
    match = UUID_RE.search(output)
    assert match, output
    return match.group(0)


def _compute(*args: str) -> Any:
    return runner.invoke(cli.app, ["compute", "-d", DS, *args])


@pytest.fixture
def ds(hive_dataset: DatasetFactory) -> DatasetIdentifier:
    return hive_dataset(rows=300)


@pytest.fixture
def logging_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(
        mode: str, level: int = logging.INFO, log_format: str | None = None, component: str = ""
    ) -> None:
        calls.append(
            {"mode": mode, "level": level, "log_format": log_format, "component": component}
        )

    monkeypatch.setattr(cli, "configure_logging", fake)
    return calls


# ----------------------------------------------------------------- comandos
def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("catalog", "compute", "plan", "status", "result", "show", "export"):
        assert command in result.output


def test_catalog_prints_published_json() -> None:
    result = runner.invoke(cli.app, ["catalog"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body == [e.model_dump(mode="json") for e in service.catalog()]


def test_compute_runs_in_process_and_prints_table(
    ds: DatasetIdentifier, settings: FinancesSettings
) -> None:
    result = _compute("-i", "rsi", "-i", "macd:fast=12,slow=26,signal=9")
    assert result.exit_code == 0, result.output
    assert "COMPLETED" in result.stdout
    task_id = task_id_from(result.stdout)
    final = service.get_result(task_id, settings)
    assert final is not None
    assert [i.series_id for i in final.items] == ["rsi_14", "macd_12_26_9"]
    assert "rsi_14" in result.stdout


def test_compute_dry_run_prints_plan_and_writes_nothing(
    ds: DatasetIdentifier, settings: FinancesSettings
) -> None:
    result = _compute("-i", "rsi", "-i", "rsi:period=14", "--dry-run")
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert [i["indicator_id"] for i in plan["items"]] == ["rsi_14"]
    assert not settings.TASKS_DB_PATH.exists()
    assert not settings.INDICATORS_DIR.exists()
    assert not (settings.SHARE_DIR / "plans").exists()


def test_compute_from_request_file(
    ds: DatasetIdentifier, settings: FinancesSettings, tmp_path: Path
) -> None:
    body = IndicatorComputeRequest(datasets=[ds], indicators=[IndicatorSpec(name="ema")])
    path = tmp_path / "req.json"
    path.write_text(body.model_dump_json(), encoding="utf-8")
    result = runner.invoke(cli.app, ["compute", "--request-file", str(path)])
    assert result.exit_code == 0, result.output
    manifest = service.get_manifest(ds, settings)
    assert manifest is not None
    assert list(manifest.entries) == ["ema_20"]


def test_plan_status_result_show_commands(ds: DatasetIdentifier) -> None:
    task_id = task_id_from(_compute("-i", "rsi").stdout)
    for command in ("plan", "status", "result"):
        out = runner.invoke(cli.app, [command, task_id])
        assert out.exit_code == 0, out.output
        assert json.loads(out.stdout)["task_id"] == task_id
    show = runner.invoke(cli.app, ["show", DS])
    assert show.exit_code == 0, show.output
    assert list(json.loads(show.stdout)["entries"]) == ["rsi_14"]


def test_export_to_output_dir(ds: DatasetIdentifier, tmp_path: Path) -> None:
    _compute("-i", "rsi", "-i", "ema")
    out = tmp_path / "exports"
    result = runner.invoke(
        cli.app,
        [
            "export",
            DS,
            "-i",
            "rsi",
            "--include-ohlcv",
            "--stability",
            "flag",
            "--format",
            "csv",
            "--start",
            "2024-01-02",
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    frame = pl.read_csv(out / "binance_spot_BTCUSDT_1h_indicators.csv")
    assert frame.columns[-2:] == ["rsi_14", "rsi_14_is_stable"]
    assert "close" in frame.columns
    assert frame["open_time_ms"][0] == JAN_2024_MS + 24 * HOUR
    assert frame.height == 300 - 24


def test_export_to_stdout_by_default(ds: DatasetIdentifier) -> None:
    _compute("-i", "rsi")
    result = runner.invoke(cli.app, ["export", DS, "--format", "csv", "--end", "2024-01-01T05:00Z"])
    assert result.exit_code == 0, result.output
    frame = pl.read_csv(io.BytesIO(result.stdout_bytes))
    assert frame.height == 5
    assert "5 filas" in result.stderr


def test_export_stale_warning_goes_to_stderr(
    ds: DatasetIdentifier, hive_dataset: DatasetFactory
) -> None:
    _compute("-i", "rsi")
    hive_dataset(rows=3, start_ms=JAN_2024_MS + 300 * HOUR, seed=3)
    result = runner.invoke(cli.app, ["export", DS, "--format", "csv", "-o", "-"])
    assert result.exit_code == 0, result.output
    assert "rsi_14" in result.stderr
    assert "desactualizad" in result.stderr


# ------------------------------------------------------------- exit codes
def test_exit_1_unknown_dataset(ds: DatasetIdentifier) -> None:
    result = runner.invoke(cli.app, ["compute", "-d", "binance/spot/NOPE/1h", "-i", "rsi"])
    assert result.exit_code == 1, result.output


def test_exit_1_locked_dataset(ds: DatasetIdentifier, settings: FinancesSettings) -> None:
    owner = service.dispatch_compute(
        IndicatorComputeRequest(datasets=[ds], indicators=[IndicatorSpec(name="rsi")]), settings
    )
    with service.get_store(settings).lock(ds, owner.task_id):
        result = _compute("-i", "ema")
    assert result.exit_code == 1, result.output
    assert owner.task_id in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["compute", "-d", DS, "-i", "nope"],
        ["compute", "-d", DS, "-i", "rsi:period=1"],
        ["compute", "-d", DS, "-i", "atr:source=open"],
        ["compute", "-d", DS, "-i", "sma:source=adj_close"],
        ["compute", "-d", "binance/spot/BTCUSDT", "-i", "rsi"],
        ["compute", "-d", DS],
        ["compute", "-i", "rsi"],
        ["show", "binance/badtype/BTCUSDT/1h"],
        ["export", DS, "--start", "not-a-date"],
    ],
)
def test_exit_2_validation(ds: DatasetIdentifier, args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output


def test_exit_2_unknown_indicator_lists_valid_names(ds: DatasetIdentifier) -> None:
    result = _compute("-i", "nope")
    assert "sma" in result.stderr
    assert "vwap" in result.stderr


def test_exit_2_request_file_combined_with_flags(ds: DatasetIdentifier, tmp_path: Path) -> None:
    path = tmp_path / "req.json"
    path.write_text("{}", encoding="utf-8")
    result = runner.invoke(cli.app, ["compute", "--request-file", str(path), "-d", DS])
    assert result.exit_code == 2, result.output
    bad = runner.invoke(cli.app, ["compute", "--request-file", str(path)])
    assert bad.exit_code == 2, bad.output


def test_exit_2_indicators_dir_inside_parquet_dir(
    ds: DatasetIdentifier, monkeypatch: pytest.MonkeyPatch, settings: FinancesSettings
) -> None:
    monkeypatch.setenv("FT_INDICATORS_DIR", str(settings.PARQUET_DIR / "indicators"))
    result = _compute("-i", "rsi")
    assert result.exit_code == 2, result.output


def test_exit_2_stability_drop_with_obv(ds: DatasetIdentifier) -> None:
    _compute("-i", "obv")
    result = runner.invoke(cli.app, ["export", DS, "--stability", "drop"])
    assert result.exit_code == 2, result.output
    assert "obv" in result.stderr


def test_exit_2_export_into_data_dir(ds: DatasetIdentifier, settings: FinancesSettings) -> None:
    _compute("-i", "rsi")
    result = runner.invoke(cli.app, ["export", DS, "-o", str(settings.INDICATORS_DIR / "x")])
    assert result.exit_code == 2, result.output


def test_exit_3_task_failed(ds: DatasetIdentifier, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        msg = "disco lleno"
        raise OSError(msg)

    monkeypatch.setattr(service, "compute_dataset", broken)
    result = _compute("-i", "rsi")
    assert result.exit_code == 3, result.output
    assert "FAILED" in result.stdout


def test_exit_3_empty_export(ds: DatasetIdentifier) -> None:
    _compute("-i", "rsi")
    result = runner.invoke(cli.app, ["export", DS, "--start", "2030-01-01"])
    assert result.exit_code == 3, result.output


def test_exit_4_unwritable_export_destination(ds: DatasetIdentifier, tmp_path: Path) -> None:
    _compute("-i", "rsi")
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    result = runner.invoke(cli.app, ["export", DS, "-o", str(blocker)])
    assert result.exit_code == 4, result.output


def test_exit_4_io_error_outside_task(
    ds: DatasetIdentifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> None:
        msg = "tasks.db de solo lectura"
        raise PermissionError(msg)

    monkeypatch.setattr(service, "create_task", broken)
    assert _compute("-i", "rsi").exit_code == 4


def test_exit_1_not_computed_export(ds: DatasetIdentifier) -> None:
    result = runner.invoke(cli.app, ["export", DS])
    assert result.exit_code == 1, result.output
    assert "compute" in result.stderr


@pytest.mark.parametrize("command", ["plan", "status", "result"])
def test_exit_1_unknown_task(command: str) -> None:
    assert runner.invoke(cli.app, [command, UNKNOWN]).exit_code == 1


def test_exit_1_unknown_manifest(ds: DatasetIdentifier) -> None:
    assert runner.invoke(cli.app, ["show", DS]).exit_code == 1


def test_exit_1_result_of_unfinished_task(
    ds: DatasetIdentifier, settings: FinancesSettings
) -> None:
    prepared = service.dispatch_compute(
        IndicatorComputeRequest(datasets=[ds], indicators=[IndicatorSpec(name="rsi")]), settings
    )
    result = runner.invoke(cli.app, ["result", prepared.task_id])
    assert result.exit_code == 1
    assert "PENDING" in result.stderr


# ----------------------------------------------------------- flags globales
def test_global_flags_configure_logging(
    ds: DatasetIdentifier, logging_calls: list[dict[str, Any]]
) -> None:
    result = runner.invoke(cli.app, ["-v", "--log-format", "json", "catalog"])
    assert result.exit_code == 0, result.output
    assert logging_calls == [
        {"mode": "cli", "level": logging.DEBUG, "log_format": "json", "component": "ft_indicators"}
    ]


def test_rich_logging_is_default(logging_calls: list[dict[str, Any]]) -> None:
    runner.invoke(cli.app, ["-q", "catalog"])
    assert logging_calls == [
        {
            "mode": "cli",
            "level": logging.WARNING,
            "log_format": "rich",
            "component": "ft_indicators",
        }
    ]


def test_invalid_log_format_is_usage_error() -> None:
    assert runner.invoke(cli.app, ["--log-format", "xml", "catalog"]).exit_code == 2
