"""CLI Typer ``ft_ta_lib`` — adaptador delgado sobre ``service`` (Art. 1; plan §2.1).

Comandos simétricos a la API (prefijo ``/ta-lib``): ``catalog`` ↔ ``GET /catalog``,
``compute`` ↔ ``POST /compute`` (aquí corre en proceso registrando la tarea en
``tasks.db``), ``plan``/``status``/``result`` ↔ ``GET /{plan,status,result}/{task_id}``,
``show`` ↔ ``GET /datasets/{...}`` y ``export`` ↔ ``POST /datasets/{...}/export``.
``catalog``/``plan``/``status``/``result``/``show`` imprimen en *stdout* el mismo JSON que
la API; logs, progreso y errores van a *stderr* (Art. 9.2).

Códigos de salida (RF-12): ``0`` OK, ``1`` no encontrado/bloqueado/genérico,
``2`` validación, ``3`` tarea ``FAILED`` o exportación vacía, ``4`` error de IO.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import NoReturn

import structlog
import typer
from finances_core.config import ConfigError, FinancesSettings
from finances_core.export import ForbiddenOutputDirError
from finances_core.indicators_store.manifest import DatasetLockedError
from finances_core.indicators_store.source import InvalidSourceError, SourceNotFoundError
from finances_core.indicators_store.store import SeriesNotFoundError
from finances_core.indicators_store.view import ExportValidationError
from finances_core.logging import LogFormat, configure_logging, level_from_flags
from finances_core.marketdata.tasks_lifecycle import TaskNotFinishedError
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import ComputeProgress, ComputeResult, SeriesExportRequest
from finances_core.schemas.ta_lib import TaLibComputeRequest
from finances_core.timeutils import parse_time_input
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import service
from .errors import EmptyExportError, TaLibValidationError
from .request import build_request, load_request_file

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

logger = structlog.get_logger(__name__)


def _out() -> Console:
    return Console()


def _err() -> Console:
    return Console(stderr=True)


def _log_format(value: str) -> LogFormat:
    for fmt in LOG_FORMATS:
        if value == fmt:
            return fmt
    msg = f"'{value}' no es válido; use rich | json"
    raise typer.BadParameter(msg, param_hint="--log-format")


def _fail(message: str, code: int) -> NoReturn:
    _err().print(message, style="red", markup=False, highlight=False)
    raise typer.Exit(code=code)


def _exit_code(exc: Exception) -> int:
    """Traducción de errores de dominio a exit codes RF-12."""
    if isinstance(exc, EmptyExportError):
        return EXIT_FAILED
    if isinstance(exc, SourceNotFoundError | SeriesNotFoundError | DatasetLockedError):
        return EXIT_GENERIC
    if isinstance(exc, InvalidSourceError):
        return EXIT_GENERIC
    if isinstance(exc, ValueError | ConfigError):  # validación (incl. pydantic, export, RF-7)
        return EXIT_VALIDATION
    if isinstance(exc, OSError):
        return EXIT_IO
    return EXIT_GENERIC


_LABELS = {
    EXIT_GENERIC: "Error",
    EXIT_VALIDATION: "Error de validación",
    EXIT_FAILED: "Sin datos",
    EXIT_IO: "Error de IO/permisos",
}


def _handle(exc: Exception) -> NoReturn:
    code = _exit_code(exc)
    if code == EXIT_GENERIC and not isinstance(exc, LookupError | DatasetLockedError):
        logger.exception("command_failed", error=str(exc))
    _fail(f"{_LABELS[code]}: {exc}", code)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Nivel WARNING"),
    log_format: str = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """ft_ta_lib: funciones de TA-Lib sobre datasets OHLCV Hive."""
    fmt = _log_format(log_format)
    configure_logging(
        "cli", level_from_flags(verbose=verbose, quiet=quiet), fmt, component=COMPONENT
    )
    ctx.obj = {"log_format": fmt, "quiet": quiet}


def _parse_dataset(value: str) -> DatasetIdentifier:
    try:
        return DatasetIdentifier.parse(value)
    except ValueError as exc:
        _fail(f"Error de validación: dataset inválido {value!r}: {exc}", EXIT_VALIDATION)


# ---------------------------------------------------------------- catalog


@app.command()
def catalog(
    group: str | None = typer.Option(None, "--group", "-g", help="Filtrar por grupo de TA-Lib"),
    function: str | None = typer.Option(None, "--function", "-f", help="Filtrar por función"),
) -> None:
    """Catálogo de TA-Lib (mismo JSON que ``GET /ta-lib/catalog``)."""
    try:
        published = service.catalog(group, function)
    except TaLibValidationError as exc:
        _handle(exc)
    typer.echo(published.model_dump_json(indent=2))


# ---------------------------------------------------------------- compute


def _compute_request(
    datasets: list[str], functions: list[str], groups: list[str], request_file: Path | None
) -> TaLibComputeRequest:
    """Petición canónica desde ``--request-file`` **o** ``--dataset``/``--function``/``--group``.

    Raises:
        TaLibValidationError: SI se combinan ambas formas o falta alguna parte.
    """
    if request_file is not None:
        if datasets or functions or groups:
            msg = "--request-file no se combina con --dataset/--function/--group"
            raise TaLibValidationError(msg)
        return load_request_file(request_file)
    if not datasets or not (functions or groups):
        msg = "Indique al menos un --dataset y un --function o --group (o --request-file)"
        raise TaLibValidationError(msg)
    return build_request(datasets, functions, groups)


@app.command()
def compute(
    ctx: typer.Context,
    datasets: list[str] | None = typer.Option(
        None, "--dataset", "-d", help="Dataset exchange/market_type/symbol/timeframe (repetible)"
    ),
    functions: list[str] | None = typer.Option(
        None,
        "--function",
        "-f",
        help="Función NAME[:k=v,...] (repetible), p. ej. rsi:timeperiod=21,price=adj_close",
    ),
    groups: list[str] | None = typer.Option(
        None, "--group", "-g", help='Grupo de TA-Lib (repetible), p. ej. "Pattern Recognition"'
    ),
    request_file: Path | None = typer.Option(
        None, "--request-file", help="JSON con exactamente el cuerpo de POST /ta-lib/compute"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo mostrar el plan (no escribe)"),
) -> None:
    """Calcula funciones de TA-Lib sobre uno o más datasets (recálculo completo, RF-1)."""
    settings = FinancesSettings()
    try:
        request = _compute_request(datasets or [], functions or [], groups or [], request_file)
        prepared = service.prepare_compute(request, settings)
        if dry_run:
            typer.echo(prepared.plan.model_dump_json(indent=2))
            return
        service.create_task(prepared, settings)
        show_progress = ctx.obj is None or ctx.obj.get("log_format") == "rich"
        result = _run_with_progress(prepared, settings, show_progress=show_progress)
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)

    _print_result(result)
    if result.status == "FAILED":
        _fail("Tarea FAILED (los archivos previos quedan intactos; re-ejecute).", EXIT_FAILED)


def _run_with_progress(
    prepared: service.PreparedCompute, settings: FinancesSettings, *, show_progress: bool
) -> ComputeResult:
    """Ejecuta la tarea en proceso con barra Rich por dataset."""
    if not show_progress:
        return asyncio.run(service.run_compute(prepared, settings))
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_err(),
    ) as progress:
        bar = progress.add_task("PENDING", total=len(prepared.items_by_dataset))

        def on_progress(p: ComputeProgress) -> None:
            progress.update(bar, completed=p.datasets_done, description=f"{p.status} datasets")

        return asyncio.run(service.run_compute(prepared, settings, on_progress=on_progress))


def _print_result(result: ComputeResult) -> None:
    out = _out()
    table = Table(title="Resultado de ft_ta_lib compute")
    table.add_column("Métrica", style="cyan")
    table.add_column("Valor", style="green")
    table.add_row("Task ID", result.task_id)
    table.add_row("Estado", result.status)
    table.add_row("Series calculadas", str(len(result.items)))
    table.add_row("Filas", str(sum(i.rows for i in result.items)))
    table.add_row("Particiones escritas", str(sum(i.partitions_written for i in result.items)))
    table.add_row("Avisos", str(len(result.warnings)))
    table.add_row("Errores", str(len(result.errors)))
    out.print(table)
    if result.items:
        detail = Table(title="Series")
        for column in ("Dataset", "series_id", "Filas", "stable_from_ms", "Avisos"):
            detail.add_column(column, overflow="fold")
        for item in result.items:
            detail.add_row(
                item.dataset.path_segment(),
                item.series_id,
                str(item.rows),
                str(item.stability.stable_from_ms),
                ", ".join(item.warnings) or "-",
            )
        out.print(detail)
    err = _err()
    for error in result.errors:
        err.print(f"• {error}", markup=False, highlight=False)


# --------------------------------------------------------- plan/status/result/show


@app.command()
def plan(task_id: str = typer.Argument(..., help="UUID de la tarea")) -> None:
    """Plan persistido (mismo JSON que ``GET /ta-lib/plan/{task_id}``)."""
    stored = service.get_plan(task_id)
    if stored is None:
        _fail(f"Plan no encontrado para {task_id}", EXIT_GENERIC)
    typer.echo(stored.model_dump_json(indent=2))


@app.command()
def status(task_id: str = typer.Argument(..., help="UUID de la tarea")) -> None:
    """Progreso (mismo JSON que ``GET /ta-lib/status/{task_id}``)."""
    progress = service.get_progress(task_id)
    if progress is None:
        _fail(f"Tarea no encontrada: {task_id}", EXIT_GENERIC)
    typer.echo(progress.model_dump_json(indent=2))


@app.command()
def result(task_id: str = typer.Argument(..., help="UUID de la tarea")) -> None:
    """Resultado final (mismo JSON que ``GET /ta-lib/result/{task_id}``)."""
    try:
        final = service.get_result(task_id)
    except TaskNotFinishedError as exc:
        _fail(f"La tarea aún no ha terminado: {exc.status}", EXIT_GENERIC)
    if final is None:
        _fail(f"Tarea no encontrada: {task_id}", EXIT_GENERIC)
    typer.echo(final.model_dump_json(indent=2))


@app.command()
def show(dataset: str = typer.Argument(..., help="exchange/market_type/symbol/timeframe")) -> None:
    """Manifiesto del dataset (mismo JSON que ``GET /ta-lib/datasets/{...}``)."""
    ds = _parse_dataset(dataset)
    try:
        manifest = service.get_manifest(ds)
    except Exception as exc:
        _handle(exc)
    if manifest is None:
        _fail(f"Sin funciones de TA-Lib calculadas para {ds.path_segment()}", EXIT_GENERIC)
    typer.echo(manifest.model_dump_json(indent=2))


# ---------------------------------------------------------------- export


@app.command()
def export(
    dataset: str = typer.Argument(..., help="exchange/market_type/symbol/timeframe"),
    functions: list[str] | None = typer.Option(
        None, "--function", "-f", help="series_id o spec NAME[:k=v,...] (repetible)"
    ),
    columns: list[str] | None = typer.Option(None, "--column", help="Columna concreta (repetible)"),
    start: str | None = typer.Option(None, "--start", help="Inicio (ISO 8601 o ms epoch)"),
    end: str | None = typer.Option(None, "--end", help="Fin exclusivo (ISO 8601 o ms epoch)"),
    include_ohlcv: bool = typer.Option(False, "--include-ohlcv", help="Añadir OHLCV fuente"),
    stability: str = typer.Option("all", "--stability", help="all | flag | drop (RF-17)"),
    fmt: str = typer.Option("parquet", "--format", help="parquet | csv | jsonl"),
    output_dir: str | None = typer.Option(
        None, "--output-dir", "-o", help="Directorio de salida, o '-' para stdout (default)"
    ),
) -> None:
    """Exporta la tabla ancha ``talib_*`` de un dataset (RF-10); solo lee lo persistido."""
    ds = _parse_dataset(dataset)
    to_stdout = output_dir is None or output_dir == "-"
    err = _err()
    try:
        req = SeriesExportRequest.model_validate(
            {
                "start_ms": parse_time_input(start) if start else None,
                "end_ms": parse_time_input(end) if end else None,
                "series": functions or None,
                "columns": columns or None,
                "include_ohlcv": include_ohlcv,
                "stability": stability,
                "format": fmt,
                "output_dir": None if to_stdout else output_dir,
            }
        )
        if to_stdout:
            sys.stdout.flush()
            outcome = service.export(ds, req, stream=sys.stdout.buffer)
            sys.stdout.buffer.flush()
        else:
            outcome = service.export(ds, req)
    except (ForbiddenOutputDirError, ExportValidationError) as exc:
        _fail(f"Error de validación: {exc}", EXIT_VALIDATION)
    except Exception as exc:
        _handle(exc)

    if outcome.stale:
        err.print(
            "Aviso: series desactualizadas (la fuente tiene velas posteriores al último "
            f"cálculo; se exporta lo persistido): {', '.join(outcome.stale)}",
            style="yellow",
            markup=False,
            highlight=False,
        )
    target = str(outcome.path) if outcome.path is not None else "stdout"
    err.print(f"Exportadas {outcome.rows} filas a {target}", style="green", highlight=False)


if __name__ == "__main__":
    app()
