"""Logging dual compartido (Art. 9): Rich para humanos, JSON estructurado para máquinas.

Única implementación de logging de la suite; cada herramienta la usa vía
``finances_core`` (Art. 4) indicando su ``component``.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Literal

import structlog
from rich.console import Console
from rich.logging import RichHandler

LogMode = Literal["cli", "api"]
LogFormat = Literal["rich", "json"]

DEFAULT_COMPONENT = "financial_tools"


def _add_service_context(
    component: str, _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Completa el contexto estructurado exigido por Art. 9.1."""
    event_dict.setdefault("component", component)
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


def configure_logging(
    mode: LogMode,
    level: int = logging.INFO,
    log_format: LogFormat | None = None,
    component: str = DEFAULT_COMPONENT,
) -> None:
    """Configura structlog según el modo de ejecución y el formato pedido.

    Args:
        mode: ``"cli"`` (logs a stderr, stdout queda para datos) o ``"api"`` (stdout).
        level: Nivel de logging estándar.
        log_format: ``"rich"`` o ``"json"``. Por defecto ``rich`` en CLI y ``json`` en API.
        component: Nombre de la herramienta, incluido en cada evento JSON.
    """
    _force_utf8_streams()
    fmt: LogFormat = log_format or ("rich" if mode == "cli" else "json")
    stream = sys.stderr if mode == "cli" else sys.stdout

    if fmt == "rich":
        console = Console(file=stream, legacy_windows=False)
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
        processors = [*_shared_processors(), structlog.dev.ConsoleRenderer(colors=True)]
    else:
        logging.basicConfig(level=level, format="%(message)s", force=True, stream=stream)
        processors = [
            *_shared_processors(),
            functools.partial(_add_service_context, component),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Sin caché: un reconfigure posterior (p. ej. en tests o subcomandos) debe aplicar.
        cache_logger_on_first_use=False,
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
