"""T5.1-T5.6 — CLI Typer (adaptador delgado sobre ``service``)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data import DownloadPlan, DownloadRequest, DownloadResult
from typer.testing import CliRunner

from finances_downloader.ft_market_data_binance import cli
from finances_downloader.ft_market_data_binance.planner import load_plan
from finances_downloader.ft_market_data_binance.service import (
    create_download_task,
    get_progress,
    new_task_id,
)

from .conftest import FakeBinance, task_id_from

runner = CliRunner()

DOWNLOAD_ARGS = [
    "download",
    "-s",
    "BTCUSDT",
    "-t",
    "1h",
    "--start",
    "2024-03-01",
    "--end",
    "2024-03-02",
]


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


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, status: str = "COMPLETED", exc: Exception | None = None
) -> list[DownloadRequest]:
    seen: list[DownloadRequest] = []

    async def fake(request: DownloadRequest, task_id: str, **_: Any) -> DownloadResult:
        seen.append(request)
        if exc is not None:
            raise exc
        return DownloadResult.model_validate(
            {"task_id": task_id, "status": status, "parquet_paths": [], "stats": {}, "errors": []}
        )

    monkeypatch.setattr(cli, "run_download", fake)
    return seen


# --------------------------------------------------------------------- T5.1
def test_import_and_help_lists_subcommands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("download", "plan", "status", "result"):
        assert command in result.output
    help_download = runner.invoke(cli.app, ["download", "--help"])
    assert "--dry-run" in help_download.output
    assert "--force" not in help_download.output  # fuera de alcance MVP (RFC futuro)


# --------------------------------------------------------------------- T5.2
def test_download_dry_run_prints_plan_json(
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport], fake_binance: FakeBinance
) -> None:
    result = runner.invoke(cli.app, [*DOWNLOAD_ARGS, "--dry-run"])
    assert result.exit_code == 0, result.output
    plan = DownloadPlan.model_validate_json(result.stdout)
    assert [i.source for i in plan.items] == ["rest_api"]
    assert load_plan(plan.task_id) == plan
    assert fake_binance.count("GET", "/klines") == 0  # nada descargado


def test_download_executes_with_phases_and_table(
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
) -> None:
    result = runner.invoke(cli.app, DOWNLOAD_ARGS)
    assert result.exit_code == 0, result.output
    assert "Resultado de descarga" in result.stdout
    assert "BTCUSDT/1h/spot" in result.stdout
    for phase in ("PLANNING", "DOWNLOADING_REST", "WRITING_PARQUET"):
        assert phase in result.stderr
    task_id = task_id_from(result.stdout)
    progress = get_progress(task_id)
    assert progress is not None
    assert progress.status == "COMPLETED"
    assert progress.processed_candles == 25


# --------------------------------------------------------------------- T5.3
def test_plan_command(use_fake_transport: Callable[[], httpx.AsyncBaseTransport]) -> None:
    dry = runner.invoke(cli.app, [*DOWNLOAD_ARGS, "--dry-run"])
    task_id = DownloadPlan.model_validate_json(dry.stdout).task_id
    shown = runner.invoke(cli.app, ["plan", task_id])
    assert shown.exit_code == 0
    assert DownloadPlan.model_validate_json(shown.stdout).task_id == task_id
    missing = runner.invoke(cli.app, ["plan", new_task_id()])
    assert missing.exit_code == 1
    assert "Plan no encontrado" in missing.stderr


# --------------------------------------------------------------------- T5.4
def test_status_command_outputs_progress_dto(settings: FinancesSettings) -> None:
    task_id = new_task_id()
    request = DownloadRequest.model_validate(
        {"symbols": ["BTCUSDT"], "timeframes": ["1h"], "start_ms": 1704067200000}
    )
    create_download_task(task_id, request, settings=settings)
    result = runner.invoke(cli.app, ["status", task_id])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    progress = get_progress(task_id, settings=settings)
    assert progress is not None
    assert payload == json.loads(progress.model_dump_json())
    assert payload["status"] == "PENDING"
    missing = runner.invoke(cli.app, ["status", new_task_id()])
    assert missing.exit_code == 1
    assert "Tarea no encontrada" in missing.stderr


def test_result_command(
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport], settings: FinancesSettings
) -> None:
    pending_id = new_task_id()
    create_download_task(
        pending_id,
        DownloadRequest.model_validate(
            {"symbols": ["BTCUSDT"], "timeframes": ["1h"], "start_ms": 1}
        ),
        settings=settings,
    )
    not_finished = runner.invoke(cli.app, ["result", pending_id])
    assert not_finished.exit_code == 1
    assert "PENDING" in not_finished.stderr
    assert runner.invoke(cli.app, ["result", new_task_id()]).exit_code == 1

    done = runner.invoke(cli.app, DOWNLOAD_ARGS)
    task_id = task_id_from(done.stdout)
    result = runner.invoke(cli.app, ["result", task_id])
    assert result.exit_code == 0
    payload = DownloadResult.model_validate_json(result.stdout)
    assert payload.status == "COMPLETED"
    assert len(payload.parquet_paths) == 1


# --------------------------------------------------------------------- T5.5
@pytest.mark.parametrize(
    ("flags", "level", "fmt"),
    [
        ([], logging.INFO, "rich"),
        (["-v"], logging.DEBUG, "rich"),
        (["--quiet"], logging.WARNING, "rich"),
        (["--log-format", "json"], logging.INFO, "json"),
    ],
)
def test_global_flags_configure_logging(
    logging_calls: list[dict[str, Any]], flags: list[str], level: int, fmt: str
) -> None:
    runner.invoke(cli.app, [*flags, "status", new_task_id()])
    assert logging_calls[0] == {
        "mode": "cli",
        "level": level,
        "log_format": fmt,
        "component": "ft_market_data_binance",
    }


def test_invalid_log_format_is_usage_error(logging_calls: list[dict[str, Any]]) -> None:
    result = runner.invoke(cli.app, ["--log-format", "xml", "status", new_task_id()])
    assert result.exit_code == 2


def test_download_level_flags_override(
    monkeypatch: pytest.MonkeyPatch, logging_calls: list[dict[str, Any]]
) -> None:
    _fake_run(monkeypatch)
    runner.invoke(cli.app, [*DOWNLOAD_ARGS, "-v", "--log-format", "json"])
    assert logging_calls[-1]["level"] == logging.DEBUG
    assert logging_calls[-1]["log_format"] == "json"


def test_json_log_format_emits_json_logs(
    use_fake_transport: Callable[[], httpx.AsyncBaseTransport],
) -> None:
    result = runner.invoke(cli.app, ["--log-format", "json", "-v", *DOWNLOAD_ARGS, "--dry-run"])
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert any(e.get("component") == "ft_market_data_binance" for e in events)


def test_max_concurrent_flag_reaches_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_run(monkeypatch)
    result = runner.invoke(cli.app, [*DOWNLOAD_ARGS, "--max-concurrent", "3", "--limit", "500"])
    assert result.exit_code == 0, result.output
    assert seen[0].max_concurrent == 3
    assert seen[0].limit_per_request == 500


# ------------------------------------------------------- T5.6 exit codes
def test_cli_exit_codes_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch)
    assert runner.invoke(cli.app, DOWNLOAD_ARGS).exit_code == 0


@pytest.mark.parametrize(
    "args",
    [
        ["download", "-s", "BTCUSDT", "-t", "1h", "--start", "2999-01-01"],
        ["download", "-s", "BTCUSDT", "-t", "7h", "--start", "2024-01-01"],
        ["download", "-s", "BTCUSDT", "-t", "1h", "--start", "ayer"],
        ["download", "-s", "BTCUSDT", "-t", "1h", "--start", "2024-02-01", "--end", "2024-01-01"],
        ["download", "-s", "BTCUSDT", "-t", "1h", "--start", "2024-01-01", "-m", "options"],
    ],
)
def test_cli_exit_codes_validation(args: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_run(monkeypatch)
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert seen == []


def test_cli_exit_codes_future_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch)
    result = runner.invoke(
        cli.app, ["download", "-s", "BTCUSDT", "-t", "1h", "--start", "2999-01-01"]
    )
    assert "Cannot download future data" in result.stderr


def test_cli_exit_codes_download_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, status="FAILED")
    assert runner.invoke(cli.app, DOWNLOAD_ARGS).exit_code == 3


def test_cli_exit_codes_io_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, exc=PermissionError("denied"))
    assert runner.invoke(cli.app, DOWNLOAD_ARGS).exit_code == 4


def test_cli_exit_codes_generic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, exc=RuntimeError("boom"))
    result = runner.invoke(cli.app, DOWNLOAD_ARGS)
    assert result.exit_code == 1
    assert "boom" in result.stderr
