"""CLI Typer de ft_data_viewer (adaptador delgado sobre ``service``, Art. 1).

Datos → stdout (Rich table / JSON / CSV); logs, progreso y errores → stderr (Art. 9.2).
Códigos de salida (T5.4): 0 OK · 1 dataset no encontrado · 2 argumentos inválidos ·
3 sin datos en rango · 4 error de IO.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn

import polars as pl
import structlog
import typer
from finances_core.logging import configure_logging, level_from_flags
from finances_core.schemas.data_viewer import DatasetIdentifier, ExportRequest
from finances_core.timeutils import parse_time_input
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from finances_downloader.ft_data_viewer import service
from finances_downloader.ft_data_viewer.analyzer import OutlierMethod, SampleMode
from finances_downloader.ft_data_viewer.errors import (
    DatasetNotFoundError,
    InvalidArgumentError,
    NoDataInRangeError,
)
from finances_downloader.ft_data_viewer.exporter import ExportFormat
from finances_downloader.ft_data_viewer.scanner import MARKET_HELP, dataset_label

COMPONENT = "ft_data_viewer"
EXIT_NOT_FOUND = 1
EXIT_INVALID_ARGS = 2
EXIT_NO_DATA = 3
EXIT_IO_ERROR = 4

OutputFormat = Literal["table", "json", "csv"]
LogFormat = Literal["rich", "json"]

app = typer.Typer(
    name="ft_data_viewer",
    help="Explorador y diagnóstico de datasets OHLCV",
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


# --- Errores → exit codes (T5.4) ------------------------------------------------------


def _fail(exc: BaseException, code: int) -> NoReturn:
    err_console.print(f"[red]Error:[/red] {escape(str(exc))}", highlight=False)
    raise typer.Exit(code=code) from exc


@contextmanager
def _cli_errors() -> Iterator[None]:
    """Traduce errores tipados del servicio a códigos de salida (sin inspeccionar mensajes)."""
    try:
        yield
    except typer.Exit:
        raise
    except DatasetNotFoundError as exc:
        _fail(exc, EXIT_NOT_FOUND)
    except InvalidArgumentError as exc:
        _fail(exc, EXIT_INVALID_ARGS)
    except NoDataInRangeError as exc:
        _fail(exc, EXIT_NO_DATA)
    except OSError as exc:
        _fail(exc, EXIT_IO_ERROR)
    except Exception as exc:
        logger.exception("unexpected_error")
        _fail(exc, EXIT_NOT_FOUND)


def _parse_time(value: str | None, flag: str) -> int | None:
    if value is None:
        return None
    try:
        return parse_time_input(value)
    except ValueError as exc:
        msg = f"{flag}: {exc}"
        raise InvalidArgumentError(msg) from exc


# --- Renderizado (T5.3) ---------------------------------------------------------------


def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _print_json(data: Any) -> None:
    console.print_json(data=data)


def _print_csv(rows: Sequence[dict[str, Any]]) -> None:
    sys.stdout.write(pl.DataFrame(list(rows)).write_csv())
    sys.stdout.flush()


def _print_table(rows: Sequence[dict[str, Any]], title: str | None = None) -> None:
    table = Table(title=title)
    for col in rows[0]:
        table.add_column(str(col), overflow="fold")
    for row in rows:
        table.add_row(*["" if v is None else str(v) for v in row.values()])
    console.print(table)


def _unsupported_csv(command: str) -> NoReturn:
    msg = f"El formato csv no está disponible para '{command}' (use table o json)"
    raise InvalidArgumentError(msg)


# --- Comandos -------------------------------------------------------------------------


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    exchange: str | None = typer.Option(None, "--exchange", "-e", help="Filtrar por exchange"),
    market_type: str | None = typer.Option(None, "--market", "-m", help=MARKET_HELP),
    symbol: str | None = typer.Option(None, "--symbol", "-s", help="Filtrar por símbolo"),
    timeframe: str | None = typer.Option(None, "--timeframe", "-t", help="Filtrar por timeframe"),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Lista datasets OHLCV disponibles en FT_PARQUET_DIR."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        if fmt == "table" and _state(ctx).show_progress:
            with Progress(
                SpinnerColumn(),
                TextColumn("Escaneando datasets"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=err_console,
                transient=True,
            ) as progress:
                task = progress.add_task("scan", total=None)
                result = service.list_datasets(
                    exchange,
                    market_type,
                    symbol,
                    timeframe,
                    on_progress=lambda done, total: progress.update(
                        task, completed=done, total=total
                    ),
                )
        else:
            result = service.list_datasets(exchange, market_type, symbol, timeframe)

        if fmt == "json":
            _print_json(result.model_dump(mode="json"))
            return
        rows = [
            {
                "exchange": d.id.exchange,
                "market_type": d.id.market_type,
                "symbol": d.id.symbol,
                "timeframe": d.id.timeframe,
                "year_min": d.year_range[0],
                "year_max": d.year_range[1],
                "month_count": d.month_count,
                "estimated_rows": d.estimated_rows,
                "size_bytes": d.size_bytes,
                "last_modified_ms": d.last_modified_ms,
                "partition_path": d.partition_path,
            }
            for d in result.datasets
        ]
        if fmt == "csv":
            if rows:
                _print_csv(rows)
            return
        if not rows:
            console.print("[yellow]No datasets found[/yellow]")
            return
        table = Table(
            title=f"Datasets OHLCV ({result.total_count}, {result.total_size_bytes / 1e6:.2f} MB)"
        )
        for col in ("Exchange", "Market", "Symbol", "TF", "Years", "Months", "Rows", "MB"):
            table.add_column(col, overflow="fold")
        for d in result.datasets:
            table.add_row(
                d.id.exchange,
                d.id.market_type,
                d.id.symbol,
                d.id.timeframe,
                f"{d.year_range[0]}-{d.year_range[1]}",
                str(d.month_count),
                f"{d.estimated_rows:,}",
                f"{d.size_bytes / 1e6:.2f}",
            )
        console.print(table)


@app.command()
def summary(
    ctx: typer.Context,
    exchange: str = typer.Option(..., "--exchange", "-e", help="Exchange (binance)"),
    market_type: str = typer.Option(..., "--market", "-m", help=MARKET_HELP),
    symbol: str = typer.Option(..., "--symbol", "-s", help="Símbolo (ej. BTCUSDT)"),
    timeframe: str = typer.Option(..., "--timeframe", "-t", help="Timeframe (ej. 1h)"),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Resumen estadístico de un dataset (metadatos Parquet + agregados lazy)."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        ident = service.make_identifier(exchange, market_type, symbol, timeframe)
        result = service.get_summary(ident)
        columns = [c.model_dump() for c in result.columns]
        if fmt == "json":
            _print_json(result.model_dump(mode="json"))
            return
        if fmt == "csv":
            _print_csv(columns)
            return
        console.print(f"[bold]Dataset:[/bold] {dataset_label(ident)}")
        console.print(f"[bold]Rows:[/bold] {result.row_count:,}")
        console.print(
            f"[bold]Date range:[/bold] {_ms_to_utc(result.date_range[0])} - "
            f"{_ms_to_utc(result.date_range[1])}"
        )
        console.print(f"[bold]Gaps detected:[/bold] {result.gaps_detected}")
        console.print(f"[bold]Duplicate timestamps:[/bold] {result.duplicate_timestamps}")
        console.print(f"[bold]Outliers (price):[/bold] {result.outlier_count}")
        _print_table(columns, title="Column Statistics")


@dataclass(frozen=True)
class RowsQuery:
    """Argumentos comunes de ``head``/``tail``/``sample``."""

    exchange: str
    market_type: str
    symbol: str
    timeframe: str
    n: int
    cols: str | None
    output_format: OutputFormat | None


def _rows_command(ctx: typer.Context, query: RowsQuery, mode: SampleMode) -> None:
    fmt = _fmt(ctx, query.output_format)
    with _cli_errors():
        ident = service.make_identifier(
            query.exchange, query.market_type, query.symbol, query.timeframe
        )
        rows = service.get_rows(ident, mode, query.n, service.parse_columns(query.cols))
        if fmt == "json":
            _print_json(rows)
        elif not rows:
            console.print("[yellow]No rows[/yellow]")
        elif fmt == "csv":
            _print_csv(rows)
        else:
            _print_table(rows)


_N_HELP = "Número de filas"
_COLS_HELP = "Columnas separadas por coma: open,high,low,close,volume"


@app.command()
def head(
    ctx: typer.Context,
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(10, "--n", "-n", min=1, max=10000, help=_N_HELP),
    cols: str | None = typer.Option(None, "--cols", help=_COLS_HELP),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Primeras N filas ordenadas por open_time_ms ASC."""
    query = RowsQuery(exchange, market_type, symbol, timeframe, n, cols, output_format)
    _rows_command(ctx, query, "head")


@app.command()
def tail(
    ctx: typer.Context,
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(10, "--n", "-n", min=1, max=10000, help=_N_HELP),
    cols: str | None = typer.Option(None, "--cols", help=_COLS_HELP),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Últimas N filas ordenadas por open_time_ms DESC."""
    query = RowsQuery(exchange, market_type, symbol, timeframe, n, cols, output_format)
    _rows_command(ctx, query, "tail")


@app.command()
def sample(
    ctx: typer.Context,
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(100, "--n", "-n", min=1, max=10000, help=_N_HELP),
    cols: str | None = typer.Option(None, "--cols", help=_COLS_HELP),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """N filas aleatorias (reservoir sampling, acotado por FT_DATA_VIEWER_SAMPLE_MAX_ROWS)."""
    query = RowsQuery(exchange, market_type, symbol, timeframe, n, cols, output_format)
    _rows_command(ctx, query, "sample")


@app.command()
def gaps(
    ctx: typer.Context,
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    min_gap_candles: int = typer.Option(
        1, "--min-gap", min=1, help="Mínimo de velas faltantes para reportar un gap"
    ),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Detecta gaps temporales en el dataset."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        ident = service.make_identifier(exchange, market_type, symbol, timeframe)
        found = service.get_gaps(ident, min_gap_candles)
        rows = [g.model_dump() for g in found]
        if fmt == "json":
            _print_json(rows)
            return
        if not rows:
            if fmt == "table":
                console.print("[green]No gaps detected[/green]")
            return
        if fmt == "csv":
            _print_csv(rows)
            return
        table = Table(title=f"Gaps detectados ({len(rows)}, min {min_gap_candles} velas)")
        for col in ("Gap Start (UTC)", "Gap End (UTC)", "Missing Candles", "Duration"):
            table.add_column(col, overflow="fold")
        for g in found:
            table.add_row(
                _ms_to_utc(g.gap_start_ms),
                _ms_to_utc(g.gap_end_ms),
                str(g.missing_candles),
                g.gap_duration_human,
            )
        console.print(table)


@app.command()
def anomalies(
    ctx: typer.Context,
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    outlier_method: OutlierMethod = typer.Option(
        "zscore", "--outlier-method", help="Método de outliers de precio: zscore | iqr"
    ),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Detecta duplicados, nulos, outliers de precio/volumen."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        if fmt == "csv":
            _unsupported_csv("anomalies")
        ident = service.make_identifier(exchange, market_type, symbol, timeframe)
        report = service.get_anomalies(ident, outlier_method)
        if fmt == "json":
            _print_json(report.model_dump(mode="json"))
            return
        console.print(f"[bold]Anomalías para {dataset_label(ident)}[/bold]")
        dup = report.duplicate_timestamps
        console.print(f"[bold]Duplicados:[/bold] {dup.get('count', 0)}")
        for ex in dup.get("examples", [])[:5]:
            console.print(f"  {_ms_to_utc(ex['timestamp'])} (count: {ex['count']})")
        console.print("[bold]Nulos:[/bold]")
        for col, count in report.null_values.items():
            console.print(f"  {col}: {count}")
        console.print(f"[bold]Outliers precio ({outlier_method}):[/bold]")
        for col, outliers in report.price_outliers.items():
            console.print(f"  {col}: {len(outliers)} outliers")
            for o in outliers[:3]:
                console.print(
                    f"    {_ms_to_utc(o['timestamp'])}: {o['value']:.6f} (z={o['zscore']:.2f})"
                )
        console.print("[bold]Anomalías volumen:[/bold]")
        for items in report.volume_anomalies.values():
            for a in items[:5]:
                console.print(f"  {_ms_to_utc(a['timestamp'])}: {a['value']} ({a['type']})")


@app.command()
def export(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    start: str | None = typer.Option(None, "--start", help="Inicio (ISO 8601 o ms epoch)"),
    end: str | None = typer.Option(None, "--end", help="Fin (ISO 8601 o ms epoch)"),
    cols: str | None = typer.Option(None, "--cols", help="Columnas separadas por coma"),
    export_format: ExportFormat = typer.Option("parquet", "--format", "-f"),
    output_dir: str | None = typer.Option(
        None, "--output-dir", "-o", help="Directorio de salida, o '-' para stdout (default)"
    ),
) -> None:
    """Exporta subconjunto filtrado a Parquet/CSV/JSONL (archivo o stdout)."""
    with _cli_errors():
        ident = service.make_identifier(exchange, market_type, symbol, timeframe)
        to_stdout = output_dir is None or output_dir == "-"
        request = ExportRequest(
            dataset_id=ident,
            start_ms=_parse_time(start, "--start"),
            end_ms=_parse_time(end, "--end"),
            columns=service.parse_columns(cols),
            format=export_format,
            output_dir=None if to_stdout else output_dir,
        )
        if to_stdout:
            sys.stdout.flush()
            outcome = service.export_data(request, stream=sys.stdout.buffer)
            sys.stdout.buffer.flush()
            err_console.print(f"[green]Exported {outcome.rows} rows to stdout[/green]")
            return
        outcome = service.export_data(request)
        console.print(f"[green]Exported {outcome.rows} rows to {outcome.path}[/green]")


@app.command()
def compare(
    ctx: typer.Context,
    exchange_a: str = typer.Option(..., "--exchange-a"),
    market_type_a: str = typer.Option(..., "--market-a"),
    symbol_a: str = typer.Option(..., "--symbol-a"),
    timeframe_a: str = typer.Option(..., "--timeframe-a"),
    exchange_b: str = typer.Option(..., "--exchange-b"),
    market_type_b: str = typer.Option(..., "--market-b"),
    symbol_b: str = typer.Option(..., "--symbol-b"),
    timeframe_b: str = typer.Option(..., "--timeframe-b"),
    start: str | None = typer.Option(None, "--start"),
    end: str | None = typer.Option(None, "--end"),
    output_format: OutputFormat | None = typer.Option(None, "--format", "-f"),
) -> None:
    """Compara dos datasets (mismo symbol/tf o distinto exchange/market)."""
    fmt = _fmt(ctx, output_format)
    with _cli_errors():
        if fmt == "csv":
            _unsupported_csv("compare")
        id_a: DatasetIdentifier = service.make_identifier(
            exchange_a, market_type_a, symbol_a, timeframe_a
        )
        id_b = service.make_identifier(exchange_b, market_type_b, symbol_b, timeframe_b)
        result = service.compare_datasets_service(
            id_a, id_b, _parse_time(start, "--start"), _parse_time(end, "--end")
        )
        if fmt == "json":
            _print_json(result.model_dump(mode="json"))
            return
        console.print(f"[bold]Comparación:[/bold] {dataset_label(id_a)} vs {dataset_label(id_b)}")
        console.print(f"  Row count diff: {result.row_count_diff:,}")
        console.print(f"  Price divergence max: {result.price_divergence_max:.6f}")
        console.print(f"  Volume divergence: {result.volume_divergence_pct:.2f}%")
        if result.date_range_overlap:
            lo, hi = result.date_range_overlap
            console.print(f"  Overlap: {_ms_to_utc(lo)} - {_ms_to_utc(hi)}")
        else:
            console.print("  Overlap: none")
        console.print(f"  Gaps only in A: {len(result.gaps_only_in_a)}")
        console.print(f"  Gaps only in B: {len(result.gaps_only_in_b)}")
