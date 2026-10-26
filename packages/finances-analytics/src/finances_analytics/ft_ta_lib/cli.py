"""CLI Typer ``ft_ta_lib`` — adaptador delgado sobre el servicio (Art. 1; plan §2.1).

Esqueleto (T0.2): flags globales ``-v/-q/--log-format``. Los comandos ``catalog``,
``compute``, ``plan``, ``status``, ``result``, ``show`` y ``export`` llegan en la Fase 4
(T4.3). Códigos de salida (RF-12): ``0`` OK, ``1`` no encontrado/bloqueado/genérico,
``2`` validación, ``3`` tarea ``FAILED`` o exportación vacía, ``4`` error de IO.
"""

from __future__ import annotations

import typer
from finances_core.logging import LogFormat, configure_logging, level_from_flags

COMPONENT = "ft_ta_lib"

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VALIDATION = 2
EXIT_FAILED = 3
EXIT_IO = 4

LOG_FORMATS: tuple[LogFormat, ...] = ("rich", "json")

app = typer.Typer(
    name="ft_ta_lib",
    help="ft_ta_lib: funciones de TA-Lib sobre datasets OHLCV Hive",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


def _log_format(value: str) -> LogFormat:
    for fmt in LOG_FORMATS:
        if value == fmt:
            return fmt
    msg = f"'{value}' no es válido; use rich | json"
    raise typer.BadParameter(msg, param_hint="--log-format")


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Nivel WARNING"),
    log_format: str = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """ft_ta_lib: funciones de TA-Lib sobre datasets OHLCV Hive."""
    fmt = _log_format(log_format)
    configure_logging(
        "cli", level_from_flags(verbose=verbose, quiet=quiet), fmt, component=COMPONENT
    )


__all__ = ["COMPONENT", "app"]
