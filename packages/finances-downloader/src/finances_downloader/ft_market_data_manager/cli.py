"""CLI Typer de ft_market_data_manager (adaptador delgado sobre ``service``, Art. 1).

Datos -> stdout (tabla Rich / JSON); logs, progreso, confirmación y errores -> stderr
(Art. 9.2). Códigos de salida (RF-10): 0 OK (incluye ``--dry-run``, ``unchanged`` y
particiones en ``conflict``) · 1 dataset no encontrado o confirmación rechazada ·
2 argumentos inválidos · 3 conflicto / confirmación faltante · 4 error de IO.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, TypeVar

import structlog
import typer
from finances_core.logging import configure_logging, level_from_flags
from finances_core.marketdata.dataset_layout import MARKET_TYPES
from finances_core.schemas.market_data_manager import (
    ImportPlan,
    ImportRequest,
    ManagerRequest,
    ManagerResult,
    PartitionResult,
    PruneRequest,
)
from finances_core.timeutils import format_time_ms
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from finances_downloader.ft_market_data_manager import service
from finances_downloader.ft_market_data_manager.errors import (
    ConfirmationRequiredError,
    ConflictError,
    DatasetNotFoundError,
    InvalidArgumentError,
)

COMPONENT = "ft_market_data_manager"
EXIT_NOT_FOUND = 1
EXIT_INVALID_ARGS = 2
EXIT_CONFLICT = 3
EXIT_IO_ERROR = 4

OutputFormat = Literal["table", "json"]
LogFormat = Literal["rich", "json"]
FileFormat = Literal["parquet", "csv", "jsonl"]
TsUnit = Literal["s", "ms", "us", "ns"]
OnConflict = Literal["skip", "fail", "replace"]
RequestT = TypeVar("RequestT", bound=ManagerRequest)
Runner = Callable[[service.PartitionCallback | None], ManagerResult]

MARKET_HELP = f"Tipo de mercado ({', '.join(sorted(MARKET_TYPES))}); repetible"

app = typer.Typer(
    name="ft_market_data_manager",
    help="Mantenimiento del almacén Parquet de velas: compactar, deduplicar y borrar particiones",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)
logger = structlog.get_logger(__name__, component=COMPONENT)


@dataclass
class CliState:
    """Opciones globales compartidas con los subcomandos vía ``ctx.obj``."""

    output_format: OutputFormat = "table"
    show_progress: bool = True


def _state(ctx: typer.Context) -> CliState:
    obj = ctx.find_object(CliState)
    return obj if obj is not None else CliState()


def _fmt(ctx: typer.Context, local: OutputFormat | None) -> OutputFormat:
    """``--format`` del subcomando > ``--format`` global > ``table``."""
    return local if local is not None else _state(ctx).output_format


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Logs nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo WARNING y superiores"),
    output_format: OutputFormat = typer.Option(
        "table", "--format", "-f", help="Formato de salida por defecto"
    ),
    log_format: LogFormat = typer.Option("rich", "--log-format", help="Formato de logs"),
) -> None:
    """Configura logging dual (Art. 9) y opciones globales."""
    configure_logging("cli", level_from_flags(verbose, quiet), log_format, component=COMPONENT)
    ctx.obj = CliState(
        output_format=output_format, show_progress=not quiet and log_format == "rich"
    )


# --- Errores -> exit codes (RF-10) --------------------------------------------------------


def _fail(exc: BaseException, code: int) -> NoReturn:
    err_console.print(f"[red]Error:[/red] {escape(str(exc))}", highlight=False)
    raise typer.Exit(code=code) from exc


@contextmanager
def _cli_errors() -> Iterator[None]:
    """Traduce errores tipados del servicio a códigos de salida (sin inspeccionar mensajes)."""
    try:
        yield
    except (typer.Exit, typer.Abort):
        raise
    except DatasetNotFoundError as exc:
        _fail(exc, EXIT_NOT_FOUND)
    except InvalidArgumentError as exc:
        _fail(exc, EXIT_INVALID_ARGS)
    except (ConflictError, ConfirmationRequiredError) as exc:
        _fail(exc, EXIT_CONFLICT)
    except OSError as exc:
        _fail(exc, EXIT_IO_ERROR)
    except Exception as exc:
        logger.exception("unexpected_error")
        _fail(exc, EXIT_IO_ERROR)


# --- Renderizado --------------------------------------------------------------------------


def _plan_table(plan: ImportPlan, title: str) -> Table:
    table = Table(title=title, show_header=False)
    table.add_column("campo", style="bold")
    table.add_column("valor", overflow="fold")
    divergence = plan.max_close_divergence
    table.add_row("destino", plan.destination)
    table.add_row(
        "rango (UTC)", f"{format_time_ms(plan.range_ms[0])} .. {format_time_ms(plan.range_ms[1])}"
    )
    table.add_row("filas en el archivo", str(plan.rows_in_file))
    table.add_row("filas nuevas", str(plan.rows_new))
    table.add_row("conflictos", str(plan.rows_conflict))
    table.add_row("filas rechazadas", str(plan.rows_rejected))
    table.add_row("particiones nuevas", ", ".join(plan.partitions_new) or "-")
    table.add_row("particiones modificadas", ", ".join(plan.partitions_modified) or "-")
    table.add_row("divergencia máx. de close", "-" if divergence is None else f"{divergence:.4%}")
    for example in plan.rejected_examples:
        table.add_row("rechazada", example)
    return table


def _render_table(result: ManagerResult) -> None:
    if result.import_plan is not None:
        console.print(_plan_table(result.import_plan, "plan de importación"))
    title = f"{result.action}{' (dry-run)' if result.dry_run else ''}"
    table = Table(title=title)
    for col in ("dataset", "period", "result", "files", "rows", "bytes", "detail"):
        table.add_column(col, overflow="fold")
    for p in result.partitions:
        table.add_row(
            p.dataset,
            f"{p.year:04d}-{p.month:02d}",
            p.result,
            f"{p.files_before} -> {p.files_after}",
            f"{p.rows_before} -> {p.rows_after}",
            f"{p.bytes_before} -> {p.bytes_after}",
            p.detail or "",
        )
    console.print(table)


def _render(result: ManagerResult, fmt: OutputFormat) -> None:
    if fmt == "json":
        console.print_json(data=result.model_dump(mode="json"))
    else:
        _render_table(result)


def _execute(ctx: typer.Context, fmt: OutputFormat, run: Runner) -> ManagerResult:
    """Ejecuta ``run`` con barra de progreso Rich (stderr) solo en modo tabla interactivo."""
    if fmt != "table" or not _state(ctx).show_progress:
        return run(None)
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=err_console,
        transient=True,
    ) as progress:
        task = progress.add_task("Procesando particiones", total=None)

        def on_partition(done: int, total: int, _result: PartitionResult) -> None:
            progress.update(task, completed=done, total=total)

        return run(on_partition)


# --- Construcción de peticiones -----------------------------------------------------------


def _selector(
    exchange: list[str] | None,
    market_type: list[str] | None,
    symbol: list[str] | None,
    timeframe: list[str] | None,
    start_ms: int | None,
) -> dict[str, object]:
    return {
        "exchange": exchange or [],
        "market_type": market_type or [],
        "symbol": symbol or [],
        "timeframe": timeframe or [],
        "start_ms": start_ms,
    }


def _build(model: type[RequestT], data: dict[str, object]) -> RequestT:
    try:
        request = model.model_validate(data)
    except ValueError as exc:  # incluye pydantic.ValidationError
        raise InvalidArgumentError(str(exc)) from exc
    service.validate_selector(request)
    return request


# --- Comandos -----------------------------------------------------------------------------


@app.command("compact")
def compact_cmd(
    ctx: typer.Context,
    exchange: list[str] | None = typer.Option(None, "--exchange", "-e", help="Exchange; repetible"),
    market_type: list[str] | None = typer.Option(None, "--market-type", "-m", help=MARKET_HELP),
    symbol: list[str] | None = typer.Option(None, "--symbol", "-s", help="Símbolo; repetible"),
    timeframe: list[str] | None = typer.Option(
        None, "--timeframe", "-t", help="Timeframe; repetible"
    ),
    start_ms: int | None = typer.Option(None, "--start-ms", help="Inicio (ms UTC, inclusive)"),
    end_ms: int | None = typer.Option(None, "--end-ms", help="Fin (ms UTC, inclusive)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Calcula el plan sin escribir"),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Unifica cada partición mensual en un único ``data.parquet`` ordenado."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        data = _selector(exchange, market_type, symbol, timeframe, start_ms)
        request = _build(ManagerRequest, {**data, "end_ms": end_ms, "dry_run": dry_run})
        _render(_execute(ctx, fmt, lambda cb: service.run_compact(request, cb)), fmt)


@app.command("dedupe")
def dedupe_cmd(
    ctx: typer.Context,
    exchange: list[str] | None = typer.Option(None, "--exchange", "-e", help="Exchange; repetible"),
    market_type: list[str] | None = typer.Option(None, "--market-type", "-m", help=MARKET_HELP),
    symbol: list[str] | None = typer.Option(None, "--symbol", "-s", help="Símbolo; repetible"),
    timeframe: list[str] | None = typer.Option(
        None, "--timeframe", "-t", help="Timeframe; repetible"
    ),
    start_ms: int | None = typer.Option(None, "--start-ms", help="Inicio (ms UTC, inclusive)"),
    end_ms: int | None = typer.Option(None, "--end-ms", help="Fin (ms UTC, inclusive)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Calcula el plan sin escribir"),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Elimina ``open_time_ms`` repetidos conservando la última fila en orden de lectura."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        data = _selector(exchange, market_type, symbol, timeframe, start_ms)
        request = _build(ManagerRequest, {**data, "end_ms": end_ms, "dry_run": dry_run})
        _render(_execute(ctx, fmt, lambda cb: service.run_dedupe(request, cb)), fmt)


def _confirm_prune(request: PruneRequest) -> None:
    """Muestra el plan y pide confirmación explícita (stderr); rechazarla aborta con exit 1."""
    plan = service.run_prune(request.model_copy(update={"dry_run": True}))
    if not plan.partitions:
        return
    rows = sum(p.rows_before for p in plan.partitions)
    size = sum(p.bytes_before for p in plan.partitions)
    err_console.print(
        f"Se borrarán {len(plan.partitions)} particiones ({rows} filas, {size} bytes).",
        highlight=False,
    )
    typer.confirm("¿Confirma el borrado?", abort=True, err=True)


@app.command("prune")
def prune_cmd(
    ctx: typer.Context,
    exchange: list[str] | None = typer.Option(None, "--exchange", "-e", help="Exchange; repetible"),
    market_type: list[str] | None = typer.Option(None, "--market-type", "-m", help=MARKET_HELP),
    symbol: list[str] | None = typer.Option(None, "--symbol", "-s", help="Símbolo; repetible"),
    timeframe: list[str] | None = typer.Option(
        None, "--timeframe", "-t", help="Timeframe; repetible"
    ),
    start_ms: int | None = typer.Option(None, "--start-ms", help="Inicio (ms UTC, inclusive)"),
    end_ms: int | None = typer.Option(None, "--end-ms", help="Fin (ms UTC, inclusive)"),
    before_ms: int | None = typer.Option(
        None, "--before-ms", help="Borra los meses completos que terminan en o antes de T (ms UTC)"
    ),
    all_partitions: bool = typer.Option(
        False, "--all", help="Borra todas las particiones seleccionadas (exige algún filtro)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Calcula el plan sin borrar"),
    force: bool = typer.Option(False, "--force", help="Omite la confirmación interactiva"),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Borra particiones mensuales completas (nunca recorta filas); pide confirmación."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        data = _selector(exchange, market_type, symbol, timeframe, start_ms)
        request = _build(
            PruneRequest,
            {
                **data,
                "end_ms": end_ms,
                "before_ms": before_ms,
                "all": all_partitions,
                "dry_run": dry_run,
                "confirm": force,
            },
        )
        if not (dry_run or force):
            _confirm_prune(request)
            request = request.model_copy(update={"confirm": True})
        _render(_execute(ctx, fmt, lambda cb: service.run_prune(request, cb)), fmt)


# --- import --------------------------------------------------------------------------------


def _confirm_import(request: ImportRequest, *, force: bool) -> None:
    """Muestra SIEMPRE la vista previa (stderr) y pide confirmación salvo ``--force``.

    Rechazar la confirmación aborta con exit 1 sin escribir nada (RF-5, RF-10).
    """
    preview = service.run_import(request.model_copy(update={"dry_run": True}))
    if preview.import_plan is not None:
        err_console.print(_plan_table(preview.import_plan, "vista previa de la importación"))
    if not force:
        typer.confirm("¿Confirma la importación?", abort=True, err=True)


@app.command("import")
def import_cmd(
    ctx: typer.Context,
    source: Path = typer.Option(
        ..., "--source", help="Archivo de origen (fuera de FT_PARQUET_DIR)"
    ),
    file_format: FileFormat | None = typer.Option(
        None, "--format", help="Formato del archivo: parquet|csv|jsonl (por defecto, la extensión)"
    ),
    exchange: str = typer.Option(..., "--exchange", "-e", help="Exchange destino"),
    market_type: str = typer.Option(..., "--market-type", "-m", help="Tipo de mercado destino"),
    symbol: str = typer.Option(..., "--symbol", "-s", help="Símbolo destino"),
    timeframe: str = typer.Option(..., "--timeframe", "-t", help="Timeframe destino"),
    create: bool = typer.Option(False, "--create", help="Permite crear el dataset destino"),
    ts_column: str = typer.Option("open_time_ms", "--ts-column", help="Columna de tiempo"),
    ts_unit: TsUnit = typer.Option("ms", "--ts-unit", help="Unidad de la columna: s|ms|us|ns"),
    on_conflict: OnConflict = typer.Option(
        "skip", "--on-conflict", help="Conflictos de open_time_ms: skip|fail|replace"
    ),
    skip_invalid: bool = typer.Option(
        False, "--skip-invalid", help="Importa las filas válidas e informa las rechazadas"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Valida y calcula el plan sin escribir"),
    force: bool = typer.Option(
        False, "--force", help="Omite la confirmación y permite replace con divergencia > 1 %"
    ),
    output_format: OutputFormat | None = typer.Option(
        None, "--output", "-o", help="Formato de salida: table|json (el global es --format)"
    ),
) -> None:
    """Importa velas de Parquet/CSV/JSONL a un dataset explícito; muestra la vista previa."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        try:
            request = ImportRequest(
                source=source,
                format=file_format,
                exchange=exchange,
                market_type=market_type,
                symbol=symbol,
                timeframe=timeframe,
                create=create,
                ts_column=ts_column,
                ts_unit=ts_unit,
                on_conflict=on_conflict,
                skip_invalid=skip_invalid,
                dry_run=dry_run,
                force=force,
                confirm=force,
            )
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from exc
        if not dry_run:
            _confirm_import(request, force=force)
            request = request.model_copy(update={"confirm": True})
        _render(_execute(ctx, fmt, lambda cb: service.run_import(request, cb)), fmt)
