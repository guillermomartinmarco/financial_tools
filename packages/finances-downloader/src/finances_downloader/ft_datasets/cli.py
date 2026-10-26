"""CLI Typer de ft_datasets (adaptador delgado, Art. 1; comandos en plan §2.1).

Datos → stdout (JSON); logs, progreso y errores → stderr (Art. 9.2).
"""

from __future__ import annotations

from typing import Literal

import typer
from finances_core.logging import configure_logging, level_from_flags

COMPONENT = "ft_datasets"

LogFormat = Literal["rich", "json"]

app = typer.Typer(
    name="ft_datasets",
    help="Recetas de datasets reales reproducibles: materializar, verificar y bloquear",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Logs nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo WARNING y superiores"),
    log_format: LogFormat = typer.Option("rich", "--log-format", help="Formato de logs"),
) -> None:
    """Configura logging dual (Art. 9) y opciones globales."""
    configure_logging("cli", level_from_flags(verbose, quiet), log_format, component=COMPONENT)
