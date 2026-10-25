import asyncio
import tempfile
from pathlib import Path
from typing import cast

import structlog
import typer
from finances_core.config import settings
from finances_core.schemas.market_data import (
    DownloadPlan,
    DownloadRequest,
    DownloadResult,
    MarketType,
    Timeframe,
)
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from finances_downloader.ft_market_data_binance.logging import configure_logging
from finances_downloader.ft_market_data_binance.planner import (
    generate_download_plan,
    parse_time_input,
)
from finances_downloader.ft_market_data_binance.service import execute_download_plan

app = typer.Typer(
    name="ft_market_data_binance",
    help="Descarga OHLCV histórica de Binance (Public Data + REST fallback)",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_DOWNLOAD = 3
EXIT_IO = 4


@app.command()
def download(
    symbols: list[str] = typer.Option(..., "--symbol", "-s", help="Símbolos (ej. BTCUSDT ETHUSDT)"),
    timeframes: list[str] = typer.Option(..., "--timeframe", "-t", help="Timeframes (ej. 1h 5m 1d)"),
    start: str = typer.Option(..., "--start", help="Inicio (ISO 8601 o ms epoch): 2024-01-01 o 1704067200000"),
    end: str | None = typer.Option(None, "--end", help="Fin (ISO 8601 o ms epoch), default=ahora"),
    market_type: str = typer.Option("spot", "--market", "-m", help="spot | futures"),
    max_concurrent: int | None = typer.Option(None, "--max-concurrent", "-c", help="Descargas simultáneas (default: FT_DOWNLOAD_MAX_CONCURRENT=5)"),
    limit: int = typer.Option(1000, "--limit", "-l", help="Velas por request REST (1-1000)"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-descargar particiones existentes (RFC futuro)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo generar plan, no ejecutar"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: str = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """Descarga OHLCV determinística: Public Data prioritario, REST fallback."""
    configure_logging("cli", verbose, quiet, log_format)
    logger = structlog.get_logger(__name__)

    try:
        # Parse timeframes
        tf_enums = [Timeframe(tf) for tf in timeframes]
        market_enum = MarketType(market_type)

        # Resolver concurrencia: CLI > env > default
        concurrent = max_concurrent or settings.DOWNLOAD_MAX_CONCURRENT
        if max_concurrent is None:
            logger.info("using_default_concurrency", value=concurrent)

        request = DownloadRequest(
            symbols=symbols,
            timeframes=tf_enums,
            start_ms=parse_time_input(start),
            end_ms=parse_time_input(end) if end else None,
            market_type=market_enum,
            limit_per_request=limit,
            max_concurrent=concurrent,
        )

        logger.info("download_request", symbols=symbols, timeframes=timeframes, market=market_type)

        # Generar plan
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Generando plan de descarga...", total=None)
            plan = await_sync(generate_download_plan(request))
            progress.update(task, completed=True)

        plan_ = cast(DownloadPlan, plan)
        if dry_run:
            console.print(f"[green]Plan generado (dry-run):[/green] {len(plan_.items)} items")
            console.print_json(plan_.model_dump_json(indent=2))
            return

        console.print(f"[green]Ejecutando plan:[/green] {len(plan_.items)} items, concurrencia={concurrent}")

        # Ejecutar plan
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Descargando...", total=None)
            result: DownloadResult = await_sync(execute_download_plan(plan_, concurrent))  # type: ignore[assignment]
            progress.update(task, completed=True)

        # Mostrar resultados
        table = Table(title="Resultado de descarga")
        table.add_column("Métrica", style="cyan")
        table.add_column("Valor", style="green")
        table.add_row("Task ID", result.task_id)
        table.add_row("Estado", result.status)
        table.add_row("Archivos Parquet", str(len(result.parquet_paths)))
        table.add_row("Total velas", str(sum(s.get("candles", 0) for s in result.stats.values())))  # type: ignore[attr-defined]
        table.add_row("Public Data items", str(sum(s.get("public", 0) for s in result.stats.values())))  # type: ignore[attr-defined]
        table.add_row("REST items", str(sum(s.get("rest", 0) for s in result.stats.values())))  # type: ignore[attr-defined]
        console.print(table)

        if result.gaps_unfillable:
            console.print(f"[yellow]Gaps no cubiertos: {len(result.gaps_unfillable)}[/yellow]")

        if result.status == "FAILED":
            console.print(f"[red]Errores fatales: {len(result.gaps_unfillable)}[/red]")
            raise typer.Exit(code=EXIT_DOWNLOAD)

    except ValueError as e:
        console.print(f"[red]Error de validación: {e}[/red]")
        raise typer.Exit(code=EXIT_VALIDATION)
    except Exception as e:
        logger.error("download_failed", error=str(e), exc_info=True)
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=EXIT_IO)


@app.command()
def plan(
    task_id: str = typer.Argument(..., help="UUID de tarea previa"),
    format: str = typer.Option("json", "--format", "-f", help="json | yaml"),
) -> None:
    """Muestra plan de descarga guardado para task_id."""
    plan_file = Path(tempfile.gettempdir()) / f"download_plan_{task_id}.json"
    if not plan_file.exists():
        console.print(f"[red]Plan no encontrado: {plan_file}[/red]")
        raise typer.Exit(code=1)

    plan = DownloadPlan.model_validate_json(plan_file.read_text())
    if format == "json":
        console.print_json(plan.model_dump_json(indent=2))
    else:
        import yaml  # type: ignore[import-untyped]
        console.print(yaml.dump(plan.model_dump()))


@app.command()
def status(
    task_id: str = typer.Argument(..., help="UUID de tarea"),
) -> None:
    """Consulta estado de tarea (polling local)."""
    from finances_core.tasks import SQLTaskManager
    task_manager = SQLTaskManager(settings.TASKS_DB_PATH)
    task = task_manager.get_task(task_id)

    if not task:
        console.print(f"[red]Tarea no encontrada: {task_id}[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"Estado de tarea {task_id}")
    table.add_column("Campo", style="cyan")
    table.add_column("Valor", style="green")
    for k, v in task.items():
        table.add_row(k, str(v))
    console.print(table)


def await_sync(coro: object) -> object:
    """Helper para correr async en CLI síncrono."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)  # type: ignore[arg-type]


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: str = typer.Option("rich", "--log-format"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
    pass


if __name__ == "__main__":
    app()
