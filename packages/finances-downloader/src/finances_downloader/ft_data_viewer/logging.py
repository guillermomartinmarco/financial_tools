import structlog
import logging
import sys
from rich.logging import RichHandler
from rich.console import Console
from typing import Literal


def configure_logging(
    mode: Literal["cli", "api"],
    verbose: bool = False,
    quiet: bool = False,
    log_format: str = "rich",
) -> None:
    """Configura structlog para CLI (Rich) o API (JSON)."""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.CallsiteParameterAdder(),
    ]

    # Force UTF-8 encoding for stdout/stderr on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    if mode == "cli" or log_format == "rich":
        console = Console(stderr=True, force_terminal=True, legacy_windows=False)
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(
                console=console,
                rich_tracebacks=True,
                markup=True,
                show_time=False,
                log_time_format="[%X]"
            )],
        )
        structlog.configure(
            processors=shared_processors + [structlog.dev.ConsoleRenderer(colors=True)],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=shared_processors + [
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )