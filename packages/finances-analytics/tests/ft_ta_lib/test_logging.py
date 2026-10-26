"""T4.5: logging dual (RF-14) — JSON con contexto estructurado y Rich por defecto en CLI."""

from __future__ import annotations

import json
from typing import Any

from finances_core.schemas.data_viewer import DatasetIdentifier
from typer.testing import CliRunner

from finances_analytics.ft_ta_lib import cli

from .conftest import DatasetFactory

runner = CliRunner()
DS = "binance/spot/BTCUSDT/1h"


def _events(stderr: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]


def test_json_logs_carry_structured_context(hive_dataset: DatasetFactory) -> None:
    ds: DatasetIdentifier = hive_dataset(rows=200)
    result = runner.invoke(
        cli.app, ["--log-format", "json", "compute", "-d", DS, "-f", "rsi", "-f", "cdlengulfing"]
    )
    assert result.exit_code == 0, result.output
    events = _events(result.stderr)
    assert events
    assert all(e["component"] == "ft_ta_lib" for e in events)

    per_function = [e for e in events if e["event"] == "function_computed"]
    assert {e["series_id"] for e in per_function} == {"talib_rsi_14", "talib_cdlengulfing"}
    assert {e["function"]: e["group"] for e in per_function} == {
        "RSI": "Momentum Indicators",
        "CDLENGULFING": "Pattern Recognition",
    }
    for event in per_function:
        assert event["dataset"] == ds.path_segment()
        assert event["symbol"] == "BTCUSDT"
        assert event["timeframe"] == "1h"
        assert len(event["task_id"]) == 36
        assert event["rows"] == 200
        assert event["compute_duration_ms"] >= 0

    dataset_event = next(e for e in events if e["event"] == "dataset_functions_computed")
    assert dataset_event["rows"] == 200
    assert dataset_event["rows_per_s"] > 0
    assert dataset_event["compute_duration_ms"] >= 0
    assert dataset_event["task_id"] == per_function[0]["task_id"]
    finished = next(e for e in events if e["event"] == "compute_task_finished")
    assert finished["rows_per_s"] > 0


def test_rich_is_default_in_cli(hive_dataset: DatasetFactory) -> None:
    hive_dataset(rows=50)
    result = runner.invoke(cli.app, ["compute", "-d", DS, "-f", "sma"])
    assert result.exit_code == 0, result.output
    assert _events(result.stderr) == []
    assert "function_computed" in result.stderr
