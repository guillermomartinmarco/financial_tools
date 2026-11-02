"""Logging dual de ``ft_strategy_manager`` (T3.1)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from ft_strategy_manager.logging import COMPONENT, configure


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


def test_api_mode_emits_json_with_component_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure("api")
    structlog.get_logger("lib").info("strategy_audit", action="create")
    captured = capsys.readouterr()
    event = json.loads(captured.out.strip().splitlines()[-1])
    assert event["event"] == "strategy_audit"
    assert event["component"] == COMPONENT
    assert event["action"] == "create"
    assert captured.err == ""


def test_cli_mode_uses_rich_on_stderr_and_keeps_stdout_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("cli")
    structlog.get_logger("lib").info("hello_world")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello_world" in captured.err
    assert not captured.err.lstrip().startswith("{")


def test_cli_can_force_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure("cli", log_format="json")
    structlog.get_logger("lib").info("forced_json")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip().splitlines()[-1])["component"] == COMPONENT


def test_verbose_and_quiet_set_levels(capsys: pytest.CaptureFixture[str]) -> None:
    configure("cli", verbose=True)
    assert logging.getLogger().level == logging.DEBUG
    structlog.get_logger("lib").debug("debug_visible")
    assert "debug_visible" in capsys.readouterr().err

    configure("cli", quiet=True)
    assert logging.getLogger().level == logging.WARNING
    structlog.get_logger("lib").info("info_hidden")
    assert "info_hidden" not in capsys.readouterr().err
