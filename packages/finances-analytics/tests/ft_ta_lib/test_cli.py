"""T4.3: CLI Typer — comandos, flags globales y exit codes RF-12 (0/1/2/3/4)."""

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
from finances_core.schemas.market_data import TF_MS_MAP
from finances_core.schemas.ta_lib import TaLibComputeRequest, TaLibSpec
from typer.testing import CliRunner

from finances_analytics.ft_ta_lib import cli, engine, service
from finances_analytics.ft_ta_lib.errors import TaLibExecutionError

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


def _request(ds: DatasetIdentifier, name: str) -> TaLibComputeRequest:
    return TaLibComputeRequest(datasets=[ds], functions=[TaLibSpec(function=name)])


# ----------------------------------------------------------------- comandos
def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("catalog", "compute", "plan", "status", "result", "show", "export"):
        assert command in result.output


def test_catalog_prints_published_json() -> None:
    result = runner.invoke(cli.app, ["catalog"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == service.catalog().model_dump(mode="json")


def test_catalog_filters() -> None:
    result = runner.invoke(cli.app, ["catalog", "--group", "Pattern Recognition"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["functions"]) == 61
    single = runner.invoke(cli.app, ["catalog", "-f", "kama"])
    assert [e["function"] for e in json.loads(single.stdout)["functions"]] == ["KAMA"]


def test_compute_group_and_functions_runs_in_process(
    ds: DatasetIdentifier, settings: FinancesSettings
) -> None:
    result = _compute("-g", "Pattern Recognition", "-f", "kama", "-f", "ht_trendline")
    assert result.exit_code == 0, result.output
    assert "COMPLETED" in result.stdout
    final = service.get_result(task_id_from(result.stdout), settings)
    assert final is not None
    sids = [i.series_id for i in final.items]
    assert len(sids) == 63
    assert sids[:2] == ["talib_kama_30", "talib_ht_trendline"]
    assert "talib_cdlengulfing" in sids


def test_compute_dry_run_prints_plan_and_writes_nothing(
    ds: DatasetIdentifier, settings: FinancesSettings
) -> None:
    result = _compute(
        "-f", "rsi", "-f", "RSI:timeperiod=14", "-f", "bbands:matype=ema", "--dry-run"
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert [i["series_id"] for i in plan["items"]] == ["talib_rsi_14", "talib_bbands_20_2_2_ema"]
    assert not settings.TASKS_DB_PATH.exists()
    assert not settings.INDICATORS_DIR.exists()
    assert not (settings.SHARE_DIR / "plans").exists()


def test_compute_from_request_file(
    ds: DatasetIdentifier, settings: FinancesSettings, tmp_path: Path
) -> None:
    path = tmp_path / "req.json"
    path.write_text(_request(ds, "ema").model_dump_json(), encoding="utf-8")
    result = runner.invoke(cli.app, ["compute", "--request-file", str(path)])
    assert result.exit_code == 0, result.output
    manifest = service.get_manifest(ds, settings)
    assert manifest is not None
    assert list(manifest.entries) == ["talib_ema_30"]


def test_plan_status_result_show_commands(ds: DatasetIdentifier) -> None:
    task_id = task_id_from(_compute("-f", "rsi").stdout)
    for command in ("plan", "status", "result"):
        out = runner.invoke(cli.app, [command, task_id])
        assert out.exit_code == 0, out.output
        assert json.loads(out.stdout)["task_id"] == task_id
    show = runner.invoke(cli.app, ["show", DS])
    assert show.exit_code == 0, show.output
    body = json.loads(show.stdout)
    assert body["namespace"] == "ft_ta_lib"
    assert list(body["entries"]) == ["talib_rsi_14"]


def test_item_errors_are_reported_and_exit_0(
    ds: DatasetIdentifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = engine.compute_function

    def fake(src: pl.DataFrame, fn: Any) -> Any:
        if fn.name == "SMA":
            msg = "SMA: fallo simulado [x]"
            raise TaLibExecutionError(msg)
        return real(src, fn)

    monkeypatch.setattr(engine, "compute_function", fake)
    result = _compute("-f", "sma", "-f", "rsi")
    assert result.exit_code == 0, result.output
    assert "talib_error" in result.stderr
    assert "fallo simulado [x]" in result.stderr


def test_export_to_output_dir(ds: DatasetIdentifier, tmp_path: Path) -> None:
    _compute("-f", "rsi", "-f", "cdlengulfing")
    out = tmp_path / "exports"
    result = runner.invoke(
        cli.app,
        [
            "export",
            DS,
            "-f",
            "rsi",
            "-f",
            "talib_cdlengulfing",
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
    frame = pl.read_csv(out / "binance_spot_BTCUSDT_1h_ta_lib.csv")
    assert "close" in frame.columns
    assert {"talib_rsi_14", "talib_cdlengulfing"} <= set(frame.columns)
    assert frame["open_time_ms"][0] == JAN_2024_MS + 24 * HOUR
    assert frame.height == 300 - 24


def test_export_to_stdout_by_default(ds: DatasetIdentifier) -> None:
    _compute("-f", "rsi")
    result = runner.invoke(cli.app, ["export", DS, "--format", "csv", "--end", "2024-01-01T05:00Z"])
    assert result.exit_code == 0, result.output
    frame = pl.read_csv(io.BytesIO(result.stdout_bytes))
    assert frame.height == 5
    assert "5 filas" in result.stderr


def test_export_stale_warning_goes_to_stderr(
    ds: DatasetIdentifier, hive_dataset: DatasetFactory
) -> None:
    _compute("-f", "rsi")
    hive_dataset(rows=3, start_ms=JAN_2024_MS + 300 * HOUR, seed=3)
    result = runner.invoke(cli.app, ["export", DS, "--format", "csv", "-o", "-"])
    assert result.exit_code == 0, result.output
    assert "talib_rsi_14" in result.stderr
    assert "desactualizad" in result.stderr


# ------------------------------------------------------------- exit codes
def test_exit_1_unknown_dataset(ds: DatasetIdentifier) -> None:
    result = runner.invoke(cli.app, ["compute", "-d", "binance/spot/NOPE/1h", "-f", "rsi"])
    assert result.exit_code == 1, result.output


def test_exit_1_locked_dataset(ds: DatasetIdentifier, settings: FinancesSettings) -> None:
    owner = service.dispatch_compute(_request(ds, "rsi"), settings)
    with service.get_store(settings).lock(ds, owner.task_id):
        result = _compute("-f", "ema")
    assert result.exit_code == 1, result.output
    assert owner.task_id in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["compute", "-d", DS, "-f", "nope"],
        ["compute", "-d", DS, "-g", "Nope"],
        ["compute", "-d", DS, "-f", "mavp"],
        ["compute", "-d", DS, "-f", "rsi:timeperiod=1"],
        ["compute", "-d", DS, "-f", "rsi:nope=3"],
        ["compute", "-d", DS, "-f", "rsi:timeperiod=1.5"],
        ["compute", "-d", DS, "-f", "atr:prices=open"],
        ["compute", "-d", DS, "-f", "sma:price=adj_close"],
        ["compute", "-d", "binance/spot/BTCUSDT", "-f", "rsi"],
        ["compute", "-d", DS],
        ["compute", "-f", "rsi"],
        ["catalog", "--group", "Nope"],
        ["catalog", "--function", "NOPE"],
        ["show", "binance/badtype/BTCUSDT/1h"],
        ["export", DS, "--start", "not-a-date"],
        ["export", DS, "-f", "rsi_14"],
    ],
)
def test_exit_2_validation(ds: DatasetIdentifier, args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output


def test_exit_2_unknown_function_lists_valid_groups(ds: DatasetIdentifier) -> None:
    result = _compute("-f", "nope")
    assert "Pattern Recognition" in result.stderr
    assert "Momentum Indicators" in result.stderr


def test_exit_2_bad_param_carries_talib_message(ds: DatasetIdentifier) -> None:
    result = _compute("-f", "rsi:timeperiod=1")
    assert result.exit_code == 2
    assert "TA_BAD_PARAM" in result.stderr


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
    assert _compute("-f", "rsi").exit_code == 2


def test_exit_2_stability_drop_with_unstable_function(ds: DatasetIdentifier) -> None:
    _compute("-f", "rsi")
    result = runner.invoke(cli.app, ["export", DS, "--stability", "drop"])
    assert result.exit_code == 2, result.output
    assert "talib_rsi_14" in result.stderr


def test_exit_2_export_into_data_dir(ds: DatasetIdentifier, settings: FinancesSettings) -> None:
    _compute("-f", "rsi")
    result = runner.invoke(cli.app, ["export", DS, "-o", str(settings.INDICATORS_DIR / "x")])
    assert result.exit_code == 2, result.output


def test_exit_3_task_failed(ds: DatasetIdentifier, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        msg = "disco lleno"
        raise OSError(msg)

    monkeypatch.setattr(service, "compute_dataset", broken)
    result = _compute("-f", "rsi")
    assert result.exit_code == 3, result.output
    assert "FAILED" in result.stdout


def test_exit_3_empty_export(ds: DatasetIdentifier) -> None:
    _compute("-f", "rsi")
    assert runner.invoke(cli.app, ["export", DS, "--start", "2030-01-01"]).exit_code == 3


def test_exit_4_unwritable_export_destination(ds: DatasetIdentifier, tmp_path: Path) -> None:
    _compute("-f", "rsi")
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    assert runner.invoke(cli.app, ["export", DS, "-o", str(blocker)]).exit_code == 4


def test_exit_4_io_error_outside_task(
    ds: DatasetIdentifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> None:
        msg = "tasks.db de solo lectura"
        raise PermissionError(msg)

    monkeypatch.setattr(service, "create_task", broken)
    assert _compute("-f", "rsi").exit_code == 4


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
    prepared = service.dispatch_compute(_request(ds, "rsi"), settings)
    result = runner.invoke(cli.app, ["result", prepared.task_id])
    assert result.exit_code == 1
    assert "PENDING" in result.stderr


# ----------------------------------------------------------- flags globales
def test_global_flags_configure_logging(logging_calls: list[dict[str, Any]]) -> None:
    result = runner.invoke(cli.app, ["-v", "--log-format", "json", "catalog", "-f", "rsi"])
    assert result.exit_code == 0, result.output
    assert logging_calls == [
        {"mode": "cli", "level": logging.DEBUG, "log_format": "json", "component": "ft_ta_lib"}
    ]


def test_rich_logging_is_default(logging_calls: list[dict[str, Any]]) -> None:
    runner.invoke(cli.app, ["-q", "catalog", "-f", "rsi"])
    assert logging_calls == [
        {"mode": "cli", "level": logging.WARNING, "log_format": "rich", "component": "ft_ta_lib"}
    ]


def test_invalid_log_format_is_usage_error() -> None:
    assert runner.invoke(cli.app, ["--log-format", "xml", "catalog"]).exit_code == 2
