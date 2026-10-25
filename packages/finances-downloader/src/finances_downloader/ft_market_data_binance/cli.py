"""CLI Typer ``ft_market_data_binance`` — adaptador delgado sobre ``service`` (Art. 1).

Comandos simétricos a la API: ``download`` ↔ ``POST /download``, ``status`` ↔
``GET /status``, ``plan`` ↔ ``GET /plan``, ``result`` ↔ ``GET /result``.
``status``/``plan``/``result`` imprimen en *stdout* el mismo JSON que la API;
logs y barras de progreso van a *stderr* (Art. 9.2).

Códigos de salida (T5.6): ``0`` OK, ``1`` error genérico, ``2`` validación,
``3`` descarga fallida/parcial, ``4`` IO/permisos.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
import typer
from finances_core.config import FinancesSettings
from finances_core.logging import LogFormat, configure_logging, level_from_flags
from finances_core.schemas.market_data import DownloadRequest, DownloadResult
from finances_core.timeutils import parse_time_input
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table

from finances_downloader.ft_market_data_binance.planner import (
    generate_download_plan,
    load_plan,
    validate_request,
)
from finances_downloader.ft_market_data_binance.service import (
    TaskNotFinishedError,
    create_download_task,
    get_progress,
    get_result,
    new_task_id,
    run_download,
)

COMPONENT = "ft_market_data_binance"

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VALIDATION = 2
EXIT_DOWNLOAD = 3
EXIT_IO = 4

LOG_FORMATS: tuple[LogFormat, ...] = ("rich", "json")

app = typer.Typer(
    name="ft_market_data_binance",
    help="Descarga OHLCV histórica de Binance (Public Data + REST fallback)",
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


def _apply_logging(verbose: bool, quiet: bool, log_format: LogFormat) -> None:
    configure_logging(
        "cli",
        level_from_flags(verbose=verbose, quiet=quiet),
        log_format,
        component=COMPONENT,
    )


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Nivel WARNING"),
    log_format: str = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
    fmt = _log_format(log_format)
    ctx.obj = {"verbose": verbose, "quiet": quiet, "log_format": fmt}
    _apply_logging(verbose, quiet, fmt)


@app.command()
def download(
    ctx: typer.Context,
    symbols: list[str] = typer.Option(..., "--symbol", "-s", help="Símbolos (ej. BTCUSDT ETHUSDT)"),
    timeframes: list[str] = typer.Option(
        ..., "--timeframe", "-t", help="Timeframes (ej. 1h 5m 1d)"
    ),
    start: str = typer.Option(
        ..., "--start", help="Inicio (ISO 8601 o ms epoch): 2024-01-01 o 1704067200000"
    ),
    end: str | None = typer.Option(None, "--end", help="Fin (ISO 8601 o ms epoch), default=ahora"),
    market_type: str = typer.Option("spot", "--market", "-m", help="spot | futures"),
    max_concurrent: int | None = typer.Option(
        None,
        "--max-concurrent",
        "-c",
        help="Descargas simultáneas (default: FT_DOWNLOAD_MAX_CONCURRENT=5)",
    ),
    limit: int = typer.Option(1000, "--limit", "-l", help="Velas por request REST (1-1000)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo generar plan, no ejecutar"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: str | None = typer.Option(None, "--log-format", help="rich | json"),
) -> None:
    """Descarga OHLCV determinística: Public Data prioritario, REST fallback."""
    globals_: dict[str, Any] = ctx.obj or {}
    if verbose or quiet or log_format is not None:
        _apply_logging(
            verbose or bool(globals_.get("verbose")),
            quiet or bool(globals_.get("quiet")),
            _log_format(log_format)
            if log_format is not None
            else globals_.get("log_format", "rich"),
        )
    err = _err()

    try:
        request = DownloadRequest.model_validate(
            {
                "symbols": [s.upper() for s in symbols],
                "timeframes": timeframes,
                "start_ms": parse_time_input(start),
                "end_ms": parse_time_input(end) if end else None,
                "market_type": market_type,
                "limit_per_request": limit,
                "max_concurrent": max_concurrent,
            }
        )
        validate_request(request)
    except ValueError as exc:  # incluye pydantic.ValidationError
        err.print(f"[red]Error de validación:[/red] {exc}")
        raise typer.Exit(code=EXIT_VALIDATION) from exc

    settings = FinancesSettings()
    task_id = new_task_id()
    try:
        if dry_run:
            plan = asyncio.run(generate_download_plan(request, task_id=task_id, settings=settings))
            typer.echo(plan.model_dump_json(indent=2))
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
        raise typer.Exit(code=EXIT_DOWNLOAD)


def _run_with_progress(
    request: DownloadRequest, task_id: str, settings: FinancesSettings, err: Console
) -> DownloadResult:
    """Ejecuta el pipeline mostrando una barra Rich por fase (RF-8)."""
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


def _print_result(result: DownloadResult) -> None:
    table = Table(title="Resultado de descarga")
    table.add_column("Métrica", style="cyan")
    table.add_column("Valor", style="green")
    table.add_row("Task ID", result.task_id)
    table.add_row("Estado", result.status)
    table.add_row("Archivos Parquet", str(len(result.parquet_paths)))
    table.add_row("Gaps no rellenables", str(len(result.gaps_unfillable)))
    out = _out()
    out.print(table)

    pairs = {k: v for k, v in result.stats.items() if isinstance(v, dict)}
    if pairs:
        detail = Table(title="Stats por símbolo/timeframe")
        for column in ("Par", "Velas", "Bytes", "Public Data", "REST"):
            detail.add_column(column)
        for key, stats in sorted(pairs.items()):
            detail.add_row(
                key,
                str(stats.get("candles", 0)),
                str(stats.get("bytes", 0)),
                str(stats.get("public", 0)),
                str(stats.get("rest", 0)),
            )
        out.print(detail)


@app.command()
def plan(task_id: str = typer.Argument(..., help="UUID de tarea previa")) -> None:
    """Muestra (JSON) el plan de descarga guardado para ``task_id``."""
    stored = load_plan(task_id)
    if stored is None:
        _err().print(f"[red]Plan no encontrado para {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    typer.echo(stored.model_dump_json(indent=2))


@app.command()
def status(task_id: str = typer.Argument(..., help="UUID de tarea")) -> None:
    """Estado de la tarea (mismo ``DownloadProgress`` que ``GET /status``)."""
    progress = get_progress(task_id)
    if progress is None:
        _err().print(f"[red]Tarea no encontrada: {task_id}[/red]")
        raise typer.Exit(code=EXIT_GENERIC)
    typer.echo(progress.model_dump_json(indent=2))


@app.command()
def result(task_id: str = typer.Argument(..., help="UUID de tarea")) -> None:
    """Resultado final (mismo ``DownloadResult`` que ``GET /result``)."""
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
