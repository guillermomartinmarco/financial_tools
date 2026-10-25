"""T4.2 — CLI Typer: comandos, flags globales y exit codes RF-9 (0/1/2/3/4)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.market_data_yfinance import (
    YFDownloadPlan,
    YFDownloadRequest,
    YFDownloadResult,
)
from typer.testing import CliRunner

from finances_downloader.ft_market_data_yfinance import cli
from finances_downloader.ft_market_data_yfinance.planner import load_plan
from finances_downloader.ft_market_data_yfinance.service import create_download_task

from .conftest import FakeYahoo, business_days, task_id_from, yahoo_frame

runner = CliRunner()

DOWNLOAD_ARGS = [
    "download",
    "-s",
    "AAPL",
    "-t",
    "1d",
    "--start",
    "2024-01-01",
    "--end",
    "2024-03-01",
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
) -> list[YFDownloadRequest]:
    seen: list[YFDownloadRequest] = []

    async def fake(request: YFDownloadRequest, task_id: str, **_: Any) -> YFDownloadResult:
        seen.append(request)
        if exc is not None:
            raise exc
        return YFDownloadResult.model_validate(
            {"task_id": task_id, "status": status, "parquet_paths": [], "stats": {}}
        )

    monkeypatch.setattr(cli, "run_download", fake)
    return seen


def _aapl(fake: FakeYahoo) -> None:
    fake.add("AAPL", "1d", yahoo_frame(business_days("AAPL", "2024-01-02", "2024-02-29")))


def test_help_lists_subcommands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("download", "plan", "status", "result"):
        assert command in result.stdout


def test_download_dry_run_prints_plan_json_without_requests(use_fake_yahoo: FakeYahoo) -> None:
    result = runner.invoke(cli.app, [*DOWNLOAD_ARGS, "--dry-run"])
    assert result.exit_code == 0, result.output
    plan = YFDownloadPlan.model_validate_json(result.stdout)
    assert [(i.symbol, i.yahoo_interval) for i in plan.items] == [("AAPL", "1d")]
    assert use_fake_yahoo.calls == []
    assert load_plan(plan.task_id) == plan


def test_download_executes_and_prints_table(use_fake_yahoo: FakeYahoo) -> None:
    _aapl(use_fake_yahoo)
    result = runner.invoke(cli.app, DOWNLOAD_ARGS)
    assert result.exit_code == 0, result.output
    task_id = task_id_from(result.stdout)
    assert "COMPLETED" in result.stdout
    assert "AAPL/1d" in result.stdout
    status = runner.invoke(cli.app, ["status", task_id])
    assert json.loads(status.stdout)["status"] == "COMPLETED"


def test_symbols_are_passed_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_run(monkeypatch)
    args = ["download", "-s", "^GSPC", "-s", "EURUSD=X", "-t", "1d", "-t", "1w"]
    result = runner.invoke(cli.app, [*args, "--start", "2024-01-01", "--max-concurrent", "3"])
    assert result.exit_code == 0, result.output
    assert seen[0].symbols == ["^GSPC", "EURUSD=X"]
    assert seen[0].timeframes == ["1d", "1w"]
    assert seen[0].max_concurrent == 3


def test_plan_status_result_commands(use_fake_yahoo: FakeYahoo) -> None:
    _aapl(use_fake_yahoo)
    task_id = task_id_from(runner.invoke(cli.app, DOWNLOAD_ARGS).stdout)
    for command in ("plan", "status", "result"):
        out = runner.invoke(cli.app, [command, task_id])
        assert out.exit_code == 0, out.output
        assert json.loads(out.stdout)["task_id"] == task_id


@pytest.mark.parametrize("command", ["plan", "status", "result"])
def test_unknown_task_is_exit_1(command: str) -> None:
    out = runner.invoke(cli.app, [command, "0192f000-0000-7000-8000-0000000000ff"])
    assert out.exit_code == 1


def test_result_of_unfinished_task_is_exit_1(settings: FinancesSettings) -> None:
    task_id = "0192f000-0000-7000-8000-0000000000c1"
    request = YFDownloadRequest(symbols=["AAPL"], timeframes=["1d"], start_ms=1, end_ms=2)
    create_download_task(task_id, request, settings=settings)
    out = runner.invoke(cli.app, ["result", task_id])
    assert out.exit_code == 1
    assert "PENDING" in out.stderr


def test_global_flags_configure_logging(
    logging_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch)
    result = runner.invoke(cli.app, ["-v", "--log-format", "json", *DOWNLOAD_ARGS])
    assert result.exit_code == 0, result.output
    assert logging_calls[0] == {
        "mode": "cli",
        "level": logging.DEBUG,
        "log_format": "json",
        "component": "ft_market_data_yfinance",
    }
    runner.invoke(cli.app, ["-q", *DOWNLOAD_ARGS])
    assert logging_calls[-1]["level"] == logging.WARNING


def test_invalid_log_format_is_usage_error(logging_calls: list[dict[str, Any]]) -> None:
    result = runner.invoke(cli.app, ["--log-format", "xml", *DOWNLOAD_ARGS])
    assert result.exit_code == 2


def test_json_logs_carry_component(use_fake_yahoo: FakeYahoo) -> None:
    result = runner.invoke(cli.app, ["--log-format", "json", "-v", *DOWNLOAD_ARGS, "--dry-run"])
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert any(e.get("component") == "ft_market_data_yfinance" for e in events)


# ------------------------------------------------------------- exit codes
def test_exit_0_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch)
    assert runner.invoke(cli.app, DOWNLOAD_ARGS).exit_code == 0


@pytest.mark.parametrize(
    "args",
    [
        ["download", "-s", "AAPL", "-t", "1d", "--start", "2999-01-01"],
        ["download", "-s", "AAPL", "-t", "4h", "--start", "2024-01-01"],
        ["download", "-s", "AAPL", "-t", "1d", "--start", "ayer"],
        ["download", "-s", "AAPL", "-t", "1d", "--start", "2024-02-01", "--end", "2024-01-01"],
        ["download", "-s", "AAPL", "-t", "1d", "--start", "2024-01-01", "--max-concurrent", "0"],
    ],
)
def test_exit_2_validation(args: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_run(monkeypatch)
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert seen == []


def test_exit_2_future_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch)
    result = runner.invoke(cli.app, ["download", "-s", "AAPL", "-t", "1d", "--start", "2999-01-01"])
    assert "Cannot download future data" in result.stderr


def test_exit_3_task_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, status="FAILED")
    assert runner.invoke(cli.app, DOWNLOAD_ARGS).exit_code == 3


def test_exit_3_with_real_persistent_429(use_fake_yahoo: FakeYahoo) -> None:
    _aapl(use_fake_yahoo)
    use_fake_yahoo.rate_limited["AAPL"] = -1
    result = runner.invoke(cli.app, DOWNLOAD_ARGS)
    assert result.exit_code == 3, result.output


def test_exit_4_io_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, exc=PermissionError("denied"))
    assert runner.invoke(cli.app, DOWNLOAD_ARGS).exit_code == 4


def test_exit_1_generic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, exc=RuntimeError("boom"))
    result = runner.invoke(cli.app, DOWNLOAD_ARGS)
    assert result.exit_code == 1
    assert "boom" in result.stderr


def test_unknown_symbol_is_exit_0_with_errors(use_fake_yahoo: FakeYahoo) -> None:
    result = runner.invoke(
        cli.app, ["download", "-s", "NOPE123", "-t", "1d", "--start", "2024-01-01"]
    )
    assert result.exit_code == 0, result.output
    task_id = task_id_from(result.stdout)
    final = json.loads(runner.invoke(cli.app, ["result", task_id]).stdout)
    assert final["status"] == "COMPLETED"
    assert "NOPE123" in final["errors"][0]
