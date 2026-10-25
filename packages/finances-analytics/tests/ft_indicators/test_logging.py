"""T5.6: logging dual (RF-13) — JSON con contexto estructurado y Rich por defecto en CLI."""

from __future__ import annotations

import json
from typing import Any

from finances_core.schemas.data_viewer import DatasetIdentifier
from typer.testing import CliRunner

from finances_analytics.ft_indicators import cli

from .conftest import DatasetFactory

runner = CliRunner()
DS = "binance/spot/BTCUSDT/1h"


def _events(stderr: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]


def test_json_logs_carry_structured_context(hive_dataset: DatasetFactory) -> None:
    ds: DatasetIdentifier = hive_dataset(rows=200)
    result = runner.invoke(
        cli.app, ["--log-format", "json", "compute", "-d", DS, "-i", "rsi", "-i", "ema"]
    )
    assert result.exit_code == 0, result.output
    events = _events(result.stderr)
    assert events
    assert all(e["component"] == "ft_indicators" for e in events)

    per_indicator = [e for e in events if e["event"] == "indicator_computed"]
    assert {e["indicator_id"] for e in per_indicator} == {"rsi_14", "ema_20"}
    for event in per_indicator:
        assert event["dataset"] == ds.path_segment()
        assert event["symbol"] == "BTCUSDT"
        assert event["timeframe"] == "1h"
        assert len(event["task_id"]) == 36
        assert event["rows"] == 200

    dataset_event = next(e for e in events if e["event"] == "dataset_indicators_computed")
    assert dataset_event["rows"] == 200
    assert dataset_event["rows_per_s"] > 0
    assert dataset_event["compute_duration_ms"] >= 0
    assert dataset_event["task_id"] == per_indicator[0]["task_id"]
    finished = next(e for e in events if e["event"] == "compute_task_finished")
    assert finished["rows_per_s"] > 0


def test_rich_is_default_in_cli(hive_dataset: DatasetFactory) -> None:
    hive_dataset(rows=50)
    result = runner.invoke(cli.app, ["compute", "-d", DS, "-i", "sma"])
    assert result.exit_code == 0, result.output
    assert _events(result.stderr) == []
    assert "indicator_computed" in result.stderr
