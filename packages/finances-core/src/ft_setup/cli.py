"""CLI de ``ft_setup`` (Typer) — adaptador delgado sobre ``service``."""

from __future__ import annotations

from typing import Literal

import typer
from rich.console import Console
from rich.table import Table

from finances_core.logging import LogFormat, configure_logging, level_from_flags
from finances_core.schemas.setup import SetupDoctorResponse
from ft_setup.service import diagnose, get_init_result, has_missing_required, run_init

app = typer.Typer(
    name="ft_setup",
    help="Inicialización y diagnóstico del entorno financial_tools",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()

EXIT_OK = 0
EXIT_REQUIRED_MISSING = 1
EXIT_INVALID = 2
EXIT_IO = 3

_COMPONENT = "ft_setup"


def _configure(verbose: bool, quiet: bool, log_format: LogFormat) -> None:
    configure_logging(
        "cli",
        level_from_flags(verbose=verbose, quiet=quiet),
        log_format=log_format,
        component=_COMPONENT,
    )


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo WARNING+"),
    log_format: LogFormat = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
    _configure(verbose, quiet, log_format)
    ctx.obj = {"verbose": verbose, "quiet": quiet, "log_format": log_format}


@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Sobrescribir archivos existentes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Mostrar qué se haría sin ejecutar"),
) -> None:
    """Inicializa estructura de directorios, .env.template y env_list.conf."""
    try:
        result = run_init(force=force, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - reporte uniforme, exit code 3
        console.print(f"[red]Init falló: {exc}[/red]")
        raise typer.Exit(code=EXIT_IO) from exc

    table = Table(title=f"ft_setup init — {result.status}")
    table.add_column("Campo", style="cyan")
    table.add_column("Valor")
    table.add_row("task_id", result.task_id)
    table.add_row("dry_run", str(dry_run))
    table.add_row("Directorios", str(len(result.created_dirs)))
    table.add_row("Archivos generados", str(len(result.generated_files)))
    for path in result.generated_files:
        table.add_row("  · archivo", path)
    console.print(table)


@app.command()
def doctor(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo WARNING+"),
    format: Literal["table", "json"] = typer.Option("table", "--format", help="table | json"),
) -> None:
    """Diagnostica variables de entorno FT_* frente a env_list.conf."""
    if verbose or quiet:
        # Solo ajusta el nivel; conserva el --log-format elegido en el callback.
        _configure(verbose, quiet, (ctx.obj or {}).get("log_format", "rich"))
    try:
        response = diagnose()
    except (OSError, ValueError) as exc:
        console.print(f"[red]No se pudo leer env_list.conf: {exc}[/red]")
        raise typer.Exit(code=EXIT_IO) from exc

    if format == "json":
        typer.echo(response.model_dump_json(indent=2))
    else:
        console.print(_doctor_table(response))

    if not response.ok:
        code = EXIT_REQUIRED_MISSING if has_missing_required(response) else EXIT_INVALID
        raise typer.Exit(code=code)


@app.command()
def status(task_id: str = typer.Argument(..., help="task_id devuelto por init")) -> None:
    """Consulta el estado de una tarea de init (equivalente a GET /setup/status/{task_id})."""
    result = get_init_result(task_id)
    if result is None:
        console.print(f"[red]Tarea no encontrada: {task_id}[/red]")
        raise typer.Exit(code=EXIT_REQUIRED_MISSING)
    typer.echo(result.model_dump_json(indent=2))


def _doctor_table(response: SetupDoctorResponse) -> Table:
    table = Table(title="Diagnóstico de variables FT_*")
    table.add_column("Variable", style="cyan", no_wrap=True)
    table.add_column("Valor")
    table.add_column("Fuente")
    table.add_column("Requerida")
    table.add_column("Estado")
    table.add_column("Detalle", overflow="fold")

    for diagnosis in response.vars:
        table.add_row(
            diagnosis.spec.name,
            diagnosis.masked_value,
            diagnosis.source.value,
            "sí" if diagnosis.spec.required else "no",
            "[green]OK[/green]" if diagnosis.valid else "[red]ERROR[/red]",
            diagnosis.validation_error or "",
        )

    summary = response.summary
    table.caption = (
        f"total={summary.get('total', 0)} · "
        f"required_ok={summary.get('required_ok', 0)} · "
        f"required_missing={summary.get('required_missing', 0)} · "
        f"invalid={summary.get('invalid', 0)} · ok={response.ok}"
    )
    return table


if __name__ == "__main__":  # pragma: no cover
    app()
