"""CLI Typer ``ft_vectorbt`` — adaptador delgado sobre ``service`` (Art. 1; RF-6, plan §2.1).

Flags globales: ``-v/--verbose``, ``-q/--quiet``, ``--log-format json|rich`` y
``--format table|json``. Los comandos (``run``, ``kinds``, ``runs``, ``show``, ``trades``,
``equity``, ``status``, ``result``) se añaden en la Fase 3. Logs y errores van a *stderr*
(Art. 9.2).
"""

from __future__ import annotations

from enum import StrEnum

import typer
from finances_core.logging import LogFormat, configure_logging, level_from_flags

COMPONENT = "ft_vectorbt"


class LogFormatChoice(StrEnum):
    """Formato de los logs de ``structlog`` (``--log-format``)."""

    RICH = "rich"
    JSON = "json"


class OutputFormat(StrEnum):
    """Formato de la salida de los comandos (``--format``)."""

    TABLE = "table"
    JSON = "json"


app = typer.Typer(
    name="ft_vectorbt",
    help="ft_vectorbt: backtesting de estrategias del núcleo con VectorBT",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Nivel WARNING"),
    log_format: LogFormatChoice = typer.Option(
        LogFormatChoice.RICH, "--log-format", help="rich | json", case_sensitive=False
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.TABLE, "--format", help="table | json", case_sensitive=False
    ),
) -> None:
    """ft_vectorbt: backtesting de estrategias del núcleo con VectorBT."""
    fmt: LogFormat = "json" if log_format is LogFormatChoice.JSON else "rich"
    configure_logging(
        "cli", level_from_flags(verbose=verbose, quiet=quiet), fmt, component=COMPONENT
    )
    ctx.obj = {"log_format": fmt, "format": output_format.value, "quiet": quiet}
