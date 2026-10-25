"""Logging dual (Art. 9): Rich para CLI, JSON estructurado para API."""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Literal

import structlog
from rich.console import Console
from rich.logging import RichHandler

LogMode = Literal["cli", "api"]

_COMPONENT = "ft_setup"


def _add_service_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Completa el contexto exigido por RF-6 para modo API."""
    event_dict.setdefault("component", _COMPONENT)
    event_dict.setdefault("trace_id", "-")
    event_dict.setdefault("span_id", "-")
    return event_dict


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.CallsiteParameterAdder(),
    ]


def configure_logging(mode: LogMode, level: int = logging.INFO) -> None:
    """Configura structlog según el modo de ejecución.

    Args:
        mode: ``"cli"`` (RichHandler a stderr) o ``"api"`` (JSON a stdout).
        level: Nivel de logging estándar (``logging.DEBUG`` y superiores).
    """
    _force_utf8_streams()

    if mode == "cli":
        console = Console(stderr=True, legacy_windows=False)
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            force=True,
            handlers=[
                RichHandler(
                    console=console,
                    rich_tracebacks=True,
                    markup=True,
                    show_time=False,
                    show_path=False,
                )
            ],
        )
        processors = _shared_processors() + [structlog.dev.ConsoleRenderer(colors=True)]
    else:
        logging.basicConfig(
            level=level,
            format="%(message)s",
            force=True,
            stream=sys.stdout,
        )
        processors = _shared_processors() + [
            _add_service_context,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _force_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def level_from_flags(verbose: bool = False, quiet: bool = False) -> int:
    """Traduce ``--verbose`` / ``--quiet`` a un nivel ``logging``."""
    if quiet:
        return logging.WARNING
    if verbose:
        return logging.DEBUG
    return logging.INFO
