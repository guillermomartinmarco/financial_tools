"""CLI Typer de ft_datasets (adaptador delgado, Art. 1; comandos en plan §2.1).

Datos → stdout (JSON o Markdown); logs, progreso y errores → stderr (Art. 9.2).

Códigos de salida (RF-9): ``0`` OK · ``1`` no encontrado/genérico · ``2`` validación ·
``3`` tarea ``FAILED`` · ``4`` IO · ``5`` verificación fallida (``MISMATCH``/``MISSING``/
``RECIPE_CHANGED``, datos no canónicos) o ``lock`` rechazado · ``6`` sin bloquear.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal, NoReturn

import structlog
import typer
from finances_core.config import FinancesSettings
from finances_core.logging import configure_logging, level_from_flags
from finances_core.schemas.datasets import MaterializeResult
from pydantic import BaseModel
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn

from finances_downloader.ft_datasets import service
from finances_downloader.ft_datasets.errors import (
    DOMAIN_ERRORS,
    EXIT_GENERIC,
    EXIT_OK,
    EXIT_VERIFY_FAILED,
    OUTCOME_EXIT,
    exit_code_for,
)
from finances_downloader.ft_datasets.runner import Runner, SubprocessRunner

COMPONENT = "ft_datasets"

LogFormat = Literal["rich", "json"]
VerifyFormat = Literal["json", "markdown"]

app = typer.Typer(
    name="ft_datasets",
    help="Recetas de datasets reales reproducibles: materializar, verificar y bloquear",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

logger = structlog.get_logger(__name__)


def make_runner() -> Runner:
    """*Runner* de descargas de ``materialize`` (los tests lo sustituyen)."""
    return SubprocessRunner()


def _err() -> Console:
    return Console(stderr=True)


def _echo(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(indent=2))


def _fail(exc: Exception) -> NoReturn:
    code = exit_code_for(exc)
    _err().print(f"[red]Error ({type(exc).__name__}):[/red] {exc}", markup=False)
    raise typer.Exit(code=code) from exc


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Logs nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo WARNING y superiores"),
    log_format: LogFormat = typer.Option("rich", "--log-format", help="Formato de logs"),
) -> None:
    """Configura logging dual (Art. 9) y opciones globales."""
    configure_logging("cli", level_from_flags(verbose, quiet), log_format, component=COMPONENT)


@app.command("list")
def list_command() -> None:
    """Recetas de FT_DATASETS_RECIPES_DIR con el estado de su lock (sin verificar datos)."""
    try:
        summaries = service.list_recipes()
    except DOMAIN_ERRORS as exc:
        _fail(exc)
    typer.echo(
        json.dumps([s.model_dump(mode="json") for s in summaries], indent=2, ensure_ascii=False)
    )


@app.command()
def show(name: str = typer.Argument(..., help="Nombre de la receta")) -> None:
    """Receta expandida (ítems y claves) y lock si existe (== GET /recipes/{name})."""
    try:
        detail = service.show_recipe(name)
    except DOMAIN_ERRORS as exc:
        _fail(exc)
    _echo(detail)


@app.command()
def materialize(
    name: str = typer.Argument(..., help="Nombre de la receta"),
    no_verify: bool = typer.Option(False, "--no-verify", help="No verificar al terminar"),
    refetch: bool = typer.Option(
        False, "--refetch", help="Borrar y re-descargar las particiones de ítems en MISMATCH"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo mostrar el plan (JSON)"),
) -> None:
    """Materializa la receta con los descargadores (tarea con progreso; nunca escribe el lock)."""
    settings = FinancesSettings()
    try:
        if dry_run:
            _echo(service.dry_run_plan(name, refetch=refetch, settings=settings))
            return
        service.prepare(name, settings)
        task_id = service.new_task_id()
        service.create_materialize_task(
            task_id, name, verify=not no_verify, refetch=refetch, settings=settings
        )
        result = _run_with_progress(name, task_id, settings, verify=not no_verify, refetch=refetch)
    except DOMAIN_ERRORS as exc:
        _fail(exc)
    _echo(result)
    code = OUTCOME_EXIT[result.outcome]
    if result.status == "FAILED":
        _err().print("[red]Materialización fallida (los datos ya escritos se conservan).[/red]")
    elif code != EXIT_OK:
        _err().print(f"[yellow]Verificación: {result.outcome}[/yellow]")
    raise typer.Exit(code=code)


def _run_with_progress(
    name: str, task_id: str, settings: FinancesSettings, *, verify: bool, refetch: bool
) -> MaterializeResult:
    """Ejecuta la tarea mostrando una línea Rich por fase (RF-8)."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=_err(),
    ) as progress:
        current: list[TaskID] = []

        def on_phase(phase: str) -> None:
            for task in current:
                progress.update(task, total=1, completed=1)
            current.clear()
            current.append(progress.add_task(phase, total=None))

        result = asyncio.run(
            service.run_materialize(
                name,
                task_id,
                settings=settings,
                runner=make_runner(),
                verify=verify,
                refetch=refetch,
                on_phase=on_phase,
            )
        )
        for task in current:
            progress.update(task, total=1, completed=1)
    return result


@app.command()
def verify(
    name: str = typer.Argument(..., help="Nombre de la receta"),
    output_format: VerifyFormat = typer.Option("json", "--format", help="json | markdown"),
) -> None:
    """Verifica el almacén contra el lock (== POST /verify/{name}); local y sin red."""
    try:
        report = service.verify(name)
    except DOMAIN_ERRORS as exc:
        _fail(exc)
    if output_format == "markdown":
        typer.echo(report.to_markdown(), nl=False)
    else:
        _echo(report)
    raise typer.Exit(code=OUTCOME_EXIT[report.status])


@app.command()
def lock(
    name: str = typer.Argument(..., help="Nombre de la receta"),
    update: bool = typer.Option(
        False, "--update", help="Reescribir un lock que difiere (revisar en un PR)"
    ),
) -> None:
    """Bloquea los hashes del almacén local (== POST /lock/{name})."""
    try:
        result = service.lock_recipe(name, update=update)
    except DOMAIN_ERRORS as exc:
        _fail(exc)
    _echo(result)
    if result.outcome == "REJECTED":
        _err().print("[red]Lock rechazado:[/red]", *result.differences, sep="\n  ", markup=False)
        raise typer.Exit(code=EXIT_VERIFY_FAILED)


@app.command()
def status(task_id: str = typer.Argument(..., help="UUID de tarea")) -> None:
    """Progreso de la tarea (== GET /status/{task_id})."""
    progress = service.get_progress(task_id)
    if progress is None:
        _err().print(f"[red]Tarea no encontrada: {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    _echo(progress)


@app.command()
def plan(task_id: str = typer.Argument(..., help="UUID de tarea")) -> None:
    """Plan persistido (== GET /plan/{task_id})."""
    stored = service.get_plan(task_id)
    if stored is None:
        _err().print(f"[red]Plan no encontrado para {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    _echo(stored)


@app.command()
def result(task_id: str = typer.Argument(..., help="UUID de tarea")) -> None:
    """Resultado final (== GET /result/{task_id})."""
    try:
        final = service.get_result(task_id)
    except service.TaskNotFinishedError as exc:
        _err().print(f"[yellow]La tarea aún no ha terminado: {exc.status}[/yellow]")
        raise typer.Exit(code=EXIT_GENERIC) from exc
    if final is None:
        _err().print(f"[red]Tarea no encontrada: {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    _echo(final)


if __name__ == "__main__":
    app()
