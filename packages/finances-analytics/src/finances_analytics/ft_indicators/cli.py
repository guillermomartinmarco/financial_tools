"""CLI Typer de ``ft_indicators`` (adaptador delgado, Art. 1; plan §2.1).

Esqueleto de la Fase 0: flags globales y configuración de logging (Art. 9.2); los
comandos se añaden en la Fase 5 (T5.3).
"""

from __future__ import annotations

from typing import Literal

import typer
from finances_core.logging import configure_logging, level_from_flags

COMPONENT = "ft_indicators"

LogFormat = Literal["rich", "json"]

app = typer.Typer(
    name="ft_indicators",
    help="ft_indicators: indicadores técnicos sobre datasets OHLCV Hive",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Nivel WARNING"),
    log_format: LogFormat = typer.Option("rich", "--log-format", help="rich|json"),
) -> None:
    """ft_indicators: indicadores técnicos sobre datasets OHLCV Hive."""
    configure_logging(
        "cli",
        level=level_from_flags(verbose=verbose, quiet=quiet),
        log_format=log_format,
        component=COMPONENT,
    )
