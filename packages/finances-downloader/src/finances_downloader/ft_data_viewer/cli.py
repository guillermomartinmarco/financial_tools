import typer
from typing import Optional, List, Literal, Dict
from pathlib import Path
import json
import time
import structlog
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.console import Console
from rich import print as rprint

from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_downloader.ft_data_viewer.service import (
    list_datasets, get_summary, get_rows, get_gaps, get_anomalies,
    export_data, compare_datasets_service, DatasetNotFoundError
)
from finances_downloader.ft_data_viewer.logging import configure_logging

app = typer.Typer(
    name="ft_data_viewer",
    help="Explorador y diagnóstico de datasets OHLCV",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()


@app.command()
def list(
    exchange: Optional[str] = typer.Option(None, "--exchange", "-e", help="Filtrar por exchange"),
    market_type: Optional[str] = typer.Option(None, "--market", "-m", help="Filtrar por market type (spot/futures)"),
    symbol: Optional[str] = typer.Option(None, "--symbol", "-s", help="Filtrar por símbolo"),
    timeframe: Optional[str] = typer.Option(None, "--timeframe", "-t", help="Filtrar por timeframe"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f", help="Formato de salida"),
) -> None:
    """Lista datasets OHLCV disponibles en FT_PARQUET_DIR."""
    try:
        result = list_datasets(exchange, market_type, symbol, timeframe)

        if format == "json":
            console.print_json(data=result.model_dump())
            return

        if result.total_count == 0:
            console.print("[yellow]No datasets found[/yellow]")
            return

        table = Table(title=f"Datasets OHLCV ({result.total_count} encontrados, {result.total_size_bytes/1e9:.2f} GB)")
        table.add_column("Exchange", style="cyan")
        table.add_column("Market", style="magenta")
        table.add_column("Symbol", style="green")
        table.add_column("Timeframe", style="yellow")
        table.add_column("Years", justify="right")
        table.add_column("Months", justify="right")
        table.add_column("Rows (est.)", justify="right")
        table.add_column("Size", justify="right")

        for d in result.datasets:
            table.add_row(
                d.id.exchange,
                d.id.market_type,
                d.id.symbol,
                d.id.timeframe,
                f"{d.year_range[0]}-{d.year_range[1]}",
                str(d.month_count),
                f"{d.estimated_rows/1e6:.1f}M",
                f"{d.size_bytes/1e9:.2f} GB",
            )
        console.print(table)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def summary(
    exchange: str = typer.Option(..., "--exchange", "-e", help="Exchange (binance)"),
    market_type: str = typer.Option(..., "--market", "-m", help="Market type (spot/futures)"),
    symbol: str = typer.Option(..., "--symbol", "-s", help="Símbolo (ej. BTCUSDT)"),
    timeframe: str = typer.Option(..., "--timeframe", "-t", help="Timeframe (ej. 1h)"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Resumen estadístico de un dataset (metadata-only, rápido)."""
    try:
        result = get_summary(exchange, market_type, symbol, timeframe)

        if format == "json":
            console.print_json(data=result.model_dump())
            return

        console.print(f"[bold]Dataset:[/bold] {exchange}/{market_type}/{symbol}/{timeframe}")
        console.print(f"[bold]Rows:[/bold] {result.row_count:,}")
        console.print(f"[bold]Date range:[/bold] {result.date_range[0]} - {result.date_range[1]}")
        console.print(f"[bold]Gaps detected:[/bold] {result.gaps_detected}")
        console.print(f"[bold]Duplicate timestamps:[/bold] {result.duplicate_timestamps}")
        console.print(f"[bold]Outliers (price):[/bold] {result.outlier_count}")

        table = Table(title="Column Statistics")
        table.add_column("Column", style="cyan")
        table.add_column("Type", style="magenta")
        table.add_column("Nulls", justify="right")
        table.add_column("Distinct", justify="right")
        table.add_column("Min", justify="right")
        table.add_column("Max", justify="right")
        table.add_column("Mean", justify="right")
        table.add_column("Std", justify="right")

        for c in result.columns:
            table.add_row(
                c.name,
                c.dtype,
                str(c.null_count),
                str(c.distinct_count) if c.distinct_count else "N/A",
                f"{c.min:.6f}" if c.min is not None else "N/A",
                f"{c.max:.6f}" if c.max is not None else "N/A",
                f"{c.mean:.6f}" if c.mean is not None else "N/A",
                f"{c.std:.6f}" if c.std is not None else "N/A",
            )
        console.print(table)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def head(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(10, "--n", "-n", min=1, max=10000),
    cols: Optional[str] = typer.Option(None, "--cols", help="Columnas separadas por coma: open,high,low,close,volume"),
    format: Literal["table", "json", "csv"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Primeras N filas ordenadas por open_time_ms ASC."""
    _print_rows(exchange, market_type, symbol, timeframe, n, cols, format, mode="head")


@app.command()
def tail(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(10, "--n", "-n", min=1, max=10000),
    cols: Optional[str] = typer.Option(None, "--cols"),
    format: Literal["table", "json", "csv"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Últimas N filas ordenadas por open_time_ms DESC."""
    _print_rows(exchange, market_type, symbol, timeframe, n, cols, format, mode="tail")


@app.command()
def sample(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    n: int = typer.Option(100, "--n", "-n", min=1, max=10000),
    cols: Optional[str] = typer.Option(None, "--cols"),
    format: Literal["table", "json", "csv"] = typer.Option("table", "--format", "-f"),
) -> None:
    """N filas aleatorias (reservoir sampling)."""
    _print_rows(exchange, market_type, symbol, timeframe, n, cols, format, mode="sample")


def _print_rows(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    n: int,
    cols: Optional[str],
    format: str,
    mode: str,
) -> None:
    """Helper para head/tail/sample."""
    try:
        col_list = [c.strip() for c in cols.split(",")] if cols else None
        rows = get_rows(exchange, market_type, symbol, timeframe, mode, n, cols)
        _print_result(rows, format)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def gaps(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    min_gap_candles: int = typer.Option(1, "--min-gap", min=1, help="Mínimo velas faltantes para reportar gap"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Detecta gaps temporales en el dataset."""
    try:
        gaps = get_gaps(exchange, market_type, symbol, timeframe, min_gap_candles)

        if format == "json":
            console.print_json(data=gaps)
            return

        if not gaps:
            console.print("[green]No gaps detected[/green]")
            return

        table = Table(title=f"Gaps detectados ({len(gaps)} gaps, min {min_gap_candles} velas)")
        table.add_column("Gap Start (UTC)", style="cyan")
        table.add_column("Gap End (UTC)", style="cyan")
        table.add_column("Missing Candles", justify="right")
        table.add_column("Duration", justify="right")

        for g in gaps:
            table.add_row(
                _ms_to_utc(g["gap_start_ms"]),
                _ms_to_utc(g["gap_end_ms"]),
                str(g["missing_candles"]),
                g["gap_duration_human"],
            )
        console.print(table)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def anomalies(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Detecta anomalías: duplicados, nulos, outliers precio/volumen."""
    try:
        report = get_anomalies(exchange, market_type, symbol, timeframe)

        if format == "json":
            console.print_json(data=report.model_dump())
            return

        console.print(f"[bold]Anomalías para {exchange}/{market_type}/{symbol}/{timeframe}[/bold]")

        # Duplicate timestamps
        dup = report.duplicate_timestamps
        console.print(f"[bold]Duplicados:[/bold] {dup.get('count', 0)}")
        for ex in dup.get("examples", [])[:5]:
            console.print(f"  {_ms_to_utc(ex['timestamp'])} (count: {ex['count']})")

        # Null values
        console.print("[bold]Nulos:[/bold]")
        for col, count in report.null_values.items():
            console.print(f"  {col}: {count}")

        # Price outliers
        console.print("[bold]Outliers precio (Z>3):[/bold]")
        for col, outliers in report.price_outliers.items():
            console.print(f"  {col}: {len(outliers)} outliers")
            for o in outliers[:3]:
                console.print(f"    {_ms_to_utc(o['timestamp'])}: {o['value']:.6f} (z={o['zscore']:.2f})")

        # Volume anomalies
        console.print("[bold]Anomalías volumen:[/bold]")
        for col, anomalies in report.volume_anomalies.items():
            for a in anomalies[:5]:
                console.print(f"  {_ms_to_utc(a['timestamp'])}: {a['value']} ({a['type']})")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def export(
    exchange: str = typer.Option(..., "--exchange", "-e"),
    market_type: str = typer.Option(..., "--market", "-m"),
    symbol: str = typer.Option(..., "--symbol", "-s"),
    timeframe: str = typer.Option(..., "--timeframe", "-t"),
    start: Optional[str] = typer.Option(None, "--start", help="Inicio (ISO 8601 o ms epoch)"),
    end: Optional[str] = typer.Option(None, "--end", help="Fin (ISO 8601 o ms epoch)"),
    cols: Optional[str] = typer.Option(None, "--cols", help="Columnas separadas por coma"),
    format: Literal["parquet", "csv", "jsonl"] = typer.Option("parquet", "--format", "-f"),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", "-o", help="Directorio salida (default: stdout)"),
) -> None:
    """Exporta subconjunto filtrado a Parquet/CSV/JSONL."""
    try:
        from finances_downloader.ft_market_data_binance.planner import parse_time_input
        col_list = [c.strip() for c in cols.split(",")] if cols else None
        start_ms = parse_time_input(start) if start else None
        end_ms = parse_time_input(end) if end else None

        result = export_data(exchange, market_type, symbol, timeframe, start_ms, end_ms, cols, format, output_dir)
        if isinstance(result, int):
            console.print(f"[green]Exported {result} rows to stdout[/green]")
        else:
            console.print(f"[green]Exported to {result}[/green]")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=2 if "No hay datos" in str(e) else 1)


@app.command()
def compare(
    exchange_a: str = typer.Option(..., "--exchange-a"),
    market_type_a: str = typer.Option(..., "--market-a"),
    symbol_a: str = typer.Option(..., "--symbol-a"),
    timeframe_a: str = typer.Option(..., "--timeframe-a"),
    exchange_b: str = typer.Option(..., "--exchange-b"),
    market_type_b: str = typer.Option(..., "--market-b"),
    symbol_b: str = typer.Option(..., "--symbol-b"),
    timeframe_b: str = typer.Option(..., "--timeframe-b"),
    start: Optional[str] = typer.Option(None, "--start"),
    end: Optional[str] = typer.Option(None, "--end"),
    format: Literal["table", "json"] = typer.Option("table", "--format", "-f"),
) -> None:
    """Compara dos datasets (mismo symbol/tf o distinto exchange/market)."""
    try:
        from finances_downloader.ft_market_data_binance.planner import parse_time_input
        from finances_core.schemas.data_viewer import DatasetIdentifier

        id_a = DatasetIdentifier(exchange=exchange_a, market_type=market_type_a, symbol=symbol_a, timeframe=timeframe_a)
        id_b = DatasetIdentifier(exchange=exchange_b, market_type=market_type_b, symbol=symbol_b, timeframe=timeframe_b)

        start_ms = parse_time_input(start) if start else None
        end_ms = parse_time_input(end) if end else None

        result = compare_datasets_service(id_a, id_b, start_ms, end_ms)

        if format == "json":
            console.print_json(data=result.model_dump())
            return

        console.print(f"[bold]Comparación:[/bold] {id_a.exchange}/{id_a.market_type}/{id_a.symbol}/{id_a.timeframe} vs {id_b.exchange}/{id_b.market_type}/{id_b.symbol}/{id_b.timeframe}")
        console.print(f"  Row count diff: {result.row_count_diff:,}")
        console.print(f"  Price divergence max: {result.price_divergence_max:.6f}")
        console.print(f"  Volume divergence: {result.volume_divergence_pct:.2f}%")
        if result.date_range_overlap:
            console.print(f"  Overlap: {_ms_to_utc(result.date_range_overlap[0])} - {_ms_to_utc(result.date_range_overlap[1])}")
        else:
            console.print("  Overlap: none")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


def _ms_to_utc(ms: int) -> str:
    """Convierte ms epoch a UTC ISO string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _print_result(rows: List[Dict], format: str) -> None:
    """Helper para imprimir filas en distintos formatos."""
    if format == "json":
        console.print_json(data=rows)
    elif format == "csv":
        if rows:
            import csv
            import io
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
            console.print(output.getvalue())
        else:
            console.print("[yellow]No rows[/yellow]")
    else:  # table
        if not rows:
            console.print("[yellow]No rows[/yellow]")
            return
        table = Table()
        for col in rows[0].keys():
            table.add_column(col)
        for row in rows:
            table.add_row(*[str(v) for v in row.values()])
        console.print(table)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: Literal["rich", "json"] = typer.Option("rich", "--log-format"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
    configure_logging("cli", verbose, quiet, log_format)