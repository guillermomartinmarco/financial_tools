"""Tests del logging dual compartido (Art. 9)."""

from __future__ import annotations

import json
import logging

import pytest
import structlog
from rich.logging import RichHandler

from finances_core.logging import configure_logging, level_from_flags


def _processor_names() -> list[str]:
    config = structlog.get_config()
    return [
        getattr(processor, "__name__", processor.__class__.__name__)
        for processor in config["processors"]
    ]


def test_cli_mode_uses_rich_handler() -> None:
    configure_logging("cli", logging.DEBUG)
    handlers = logging.getLogger().handlers
    assert any(isinstance(handler, RichHandler) for handler in handlers)
    assert "ConsoleRenderer" in " ".join(_processor_names())


def test_api_mode_uses_json_renderer(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api", logging.INFO, component="ft_setup")
    assert "JSONRenderer" in " ".join(_processor_names())

    structlog.get_logger("test").info("evento_prueba", task_id="abc")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "evento_prueba"
    assert payload["component"] == "ft_setup"
    assert payload["task_id"] == "abc"
    assert payload["trace_id"] == "-"
    assert payload["span_id"] == "-"


def test_cli_mode_with_json_format_emits_json_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("cli", logging.INFO, log_format="json", component="ft_data_viewer")
    assert "JSONRenderer" in " ".join(_processor_names())

    structlog.get_logger("test").info("evento_cli")
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err.strip())
    assert payload["event"] == "evento_cli"
    assert payload["component"] == "ft_data_viewer"


@pytest.mark.parametrize(
    ("verbose", "quiet", "expected"),
    [
        (False, False, logging.INFO),
        (True, False, logging.DEBUG),
        (False, True, logging.WARNING),
        (True, True, logging.WARNING),
    ],
)
def test_level_from_flags(verbose: bool, quiet: bool, expected: int) -> None:
    assert level_from_flags(verbose=verbose, quiet=quiet) == expected
