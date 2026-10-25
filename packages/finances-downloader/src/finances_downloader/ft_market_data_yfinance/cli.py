"""CLI Typer ``ft_market_data_yfinance`` — adaptador delgado sobre ``service`` (Art. 1).

Comandos simétricos a la API: ``download`` ↔ ``POST /download``, ``status`` ↔
``GET /status``, ``plan`` ↔ ``GET /plan``, ``result`` ↔ ``GET /result``.
``status``/``plan``/``result`` imprimen en *stdout* el mismo JSON que la API;
logs y barras de progreso van a *stderr* (Art. 9.2).

Códigos de salida (RF-9): ``0`` OK, ``1`` no encontrado/genérico, ``2`` validación,
``3`` tarea ``FAILED``, ``4`` IO/permisos.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
import typer
from finances_core.config import FinancesSettings
from finances_core.logging import LogFormat, configure_logging, level_from_flags
from finances_core.schemas.market_data_yfinance import YFDownloadRequest, YFDownloadResult
from finances_core.timeutils import parse_time_input
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table

from finances_downloader.ft_market_data_yfinance.planner import (
    build_download_plan,
    load_plan,
    validate_request,
)
from finances_downloader.ft_market_data_yfinance.service import (
    TaskNotFinishedError,
    create_download_task,
    get_progress,
    get_result,
    new_task_id,
    run_download,
)

COMPONENT = "ft_market_data_yfinance"

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VALIDATION = 2
EXIT_FAILED = 3
EXIT_IO = 4

LOG_FORMATS: tuple[LogFormat, ...] = ("rich", "json")

app = typer.Typer(
    name="ft_market_data_yfinance",
    help="Descarga OHLCV (precios crudos + adj_close) y eventos corporativos de Yahoo Finance",
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


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Nivel WARNING"),
    log_format: str = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
    configure_logging(
        "cli",
        level_from_flags(verbose=verbose, quiet=quiet),
        _log_format(log_format),
        component=COMPONENT,
    )


def _parse_request(
    symbols: list[str],
    timeframes: list[str],
    start: str,
    end: str | None,
    max_concurrent: int | None,
) -> YFDownloadRequest:
    request = YFDownloadRequest.model_validate(
        {
            "symbols": symbols,
            "timeframes": timeframes,
            "start_ms": parse_time_input(start),
            "end_ms": parse_time_input(end) if end else None,
            "max_concurrent": max_concurrent,
        }
    )
    validate_request(request)
    return request


@app.command()
def download(
    symbols: list[str] = typer.Option(
        ..., "--symbol", "-s", help="Símbolos tal cual Yahoo (ej. AAPL ^GSPC EURUSD=X GC=F)"
    ),
    timeframes: list[str] = typer.Option(
        ..., "--timeframe", "-t", help="1m 5m 15m 30m 1h 1d 1w 1M"
    ),
    start: str = typer.Option(..., "--start", help="Inicio (ISO 8601, ms epoch o 'now')"),
    end: str | None = typer.Option(None, "--end", help="Fin (ISO 8601, ms epoch), default=ahora"),
    max_concurrent: int | None = typer.Option(
        None,
        "--max-concurrent",
        "-c",
        help="Pedidos simultáneos (default: FT_DOWNLOAD_MAX_CONCURRENT=5)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Sólo generar y mostrar el plan"),
) -> None:
    """Descarga incremental y sin pérdida desde Yahoo Finance (RF-1)."""
    err = _err()
    try:
        request = _parse_request(symbols, timeframes, start, end, max_concurrent)
    except ValueError as exc:  # incluye pydantic.ValidationError
        err.print(f"[red]Error de validación:[/red] {exc}")
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    settings = FinancesSettings()
    task_id = new_task_id()
    try:
        if dry_run:
            outcome = build_download_plan(request, task_id=task_id, settings=settings)
            typer.echo(outcome.plan.model_dump_json(indent=2))
            return
        create_download_task(task_id, request, settings=settings)
        result = _run_with_progress(request, task_id, settings, err)
    except OSError as exc:
        logger.exception("download_io_error", error=str(exc))
        err.print(f"[red]Error de IO/permisos:[/red] {exc}")
        raise typer.Exit(code=EXIT_IO) from exc
    except ValueError as exc:
        err.print(f"[red]Error de validación:[/red] {exc}")
        raise typer.Exit(code=EXIT_VALIDATION) from exc
    except Exception as exc:
        logger.exception("download_failed", error=str(exc))
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=EXIT_GENERIC) from exc

    _print_result(result)
    if result.status == "FAILED":
        err.print("[red]Descarga fallida (los datos ya escritos se conservan).[/red]")
        raise typer.Exit(code=EXIT_FAILED)


def _run_with_progress(
    request: YFDownloadRequest, task_id: str, settings: FinancesSettings, err: Console
) -> YFDownloadResult:
    """Ejecuta el pipeline mostrando una línea Rich por fase (RF-8)."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=err,
    ) as progress:
        current: list[TaskID] = []

        def on_phase(phase: str) -> None:
            for task in current:
                progress.update(task, total=1, completed=1)
            current.clear()
            current.append(progress.add_task(phase, total=None))

        result = asyncio.run(run_download(request, task_id, settings=settings, on_phase=on_phase))
        for task in current:
            progress.update(task, total=1, completed=1)
    return result


def _print_result(result: YFDownloadResult) -> None:
    table = Table(title="Resultado de descarga")
    table.add_column("Métrica", style="cyan")
    table.add_column("Valor", style="green")
    table.add_row("Task ID", result.task_id)
    table.add_row("Estado", result.status)
    table.add_row("Archivos Parquet", str(len(result.parquet_paths)))
    table.add_row("Archivos de eventos", str(len(result.events_paths)))
    table.add_row("Gaps fuera de historial", str(len(result.gaps_unfillable)))
    table.add_row("Errores", str(len(result.errors)))
    out = _out()
    out.print(table)

    pairs: dict[str, dict[str, Any]] = {
        k: v for k, v in result.stats.items() if isinstance(v, dict)
    }
    if pairs:
        detail = Table(title="Stats por símbolo/timeframe")
        for column in ("Par", "Market type", "Velas", "Pedidos"):
            detail.add_column(column)
        for key, stats in sorted(pairs.items()):
            detail.add_row(
                key,
                str(stats.get("market_type") or "-"),
                str(stats.get("candles", 0)),
                str(stats.get("requests", 0)),
            )
        out.print(detail)
    for error in result.errors:
        _err().print(f"[yellow]•[/yellow] {error}")


@app.command()
def plan(task_id: str = typer.Argument(..., help="UUID de tarea previa")) -> None:
    """Plan de descarga guardado (mismo JSON que ``GET /plan``)."""
    stored = load_plan(task_id)
    if stored is None:
        _err().print(f"[red]Plan no encontrado para {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    typer.echo(stored.model_dump_json(indent=2))


@app.command()
def status(task_id: str = typer.Argument(..., help="UUID de tarea")) -> None:
    """Progreso de la tarea (mismo JSON que ``GET /status``)."""
    progress = get_progress(task_id)
    if progress is None:
        _err().print(f"[red]Tarea no encontrada: {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    typer.echo(progress.model_dump_json(indent=2))


@app.command()
def result(task_id: str = typer.Argument(..., help="UUID de tarea")) -> None:
    """Resultado final (mismo JSON que ``GET /result``)."""
    try:
        final = get_result(task_id)
    except TaskNotFinishedError as exc:
        _err().print(f"[yellow]La tarea aún no ha terminado: {exc.status}[/yellow]")
        raise typer.Exit(code=EXIT_GENERIC) from exc
    if final is None:
        _err().print(f"[red]Tarea no encontrada: {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    typer.echo(final.model_dump_json(indent=2))


if __name__ == "__main__":
    app()
