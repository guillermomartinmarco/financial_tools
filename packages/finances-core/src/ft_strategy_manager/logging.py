"""Logging dual de ``ft_strategy_manager`` (Art. 9): fina capa sobre ``finances_core.logging``.

No reimplementa nada: fija el ``component`` de la herramienta y traduce los flags
``--verbose/--quiet/--log-format`` de la CLI o el modo servicio de la API.
"""

from __future__ import annotations

from finances_core.logging import LogFormat, LogMode, configure_logging, level_from_flags

COMPONENT = "ft_strategy_manager"


def configure(
    mode: LogMode,
    verbose: bool = False,
    quiet: bool = False,
    log_format: LogFormat | None = None,
) -> None:
    """Configura structlog: Rich en stderr para ``cli``, JSON en stdout para ``api``."""
    configure_logging(
        mode,
        level_from_flags(verbose=verbose, quiet=quiet),
        log_format=log_format,
        component=COMPONENT,
    )
