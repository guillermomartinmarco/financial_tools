"""CLI Typer ``ft_vectorbt`` — adaptador delgado sobre ``service`` (Art. 1; RF-6, plan §2.1).

Flags globales: ``-v/--verbose``, ``-q/--quiet``, ``--log-format json|rich`` y
``--format table|json``. Comandos simétricos a la API (prefijo ``/backtest/vectorbt``):
``run`` ↔ ``POST /run`` (aquí corre en proceso registrando fases en una barra Rich) y
``kinds`` ↔ ``GET /kinds`` (``--check`` es solo CLI, RF-6). *stdout* lleva el JSON o la
tabla del resultado; logs, progreso y errores van a *stderr* (Art. 9.2).

Códigos de salida (RF-9 / ``finances_backtester`` RF-14): ``0`` OK (incluido ``cached``),
``1`` no encontrado/genérico, ``2`` validación (``unknown_kind``, ``invalid_params``,
``strategy_init_error`` local, costes o rango inválidos), ``3`` tarea ``FAILED`` y ``4``
error de IO.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn

import structlog
import typer
from finances_core.backtest.errors import BacktestError
from finances_core.backtest.results_store import StoredRun
from finances_core.config import FinancesSettings
from finances_core.indicators_store.source import SourceNotFoundError
from finances_core.logging import LogFormat, configure_logging, level_from_flags
from finances_core.marketdata.tasks_lifecycle import TaskNotFinishedError
from finances_core.schemas.backtest import (
    BacktestDataset,
    BacktestRequest,
    BacktestTaskResult,
    EquityPage,
    KindInfo,
    RunsPage,
    TradesPage,
)
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import service

COMPONENT = "ft_vectorbt"

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VALIDATION = 2
EXIT_FAILED = 3
EXIT_IO = 4

#: Fases no terminales reportadas por el ``runner`` (plan §3.1); total de la barra Rich.
_RUN_PHASES = 5


class LogFormatChoice(StrEnum):
    """Formato de los logs de ``structlog`` (``--log-format``)."""

    RICH = "rich"
    JSON = "json"


class OutputFormat(StrEnum):
    """Formato de la salida de los comandos (``--format``)."""

    TABLE = "table"
    JSON = "json"


app = typer.Typer(
    name="ft_vectorbt",
    help="ft_vectorbt: backtesting de estrategias del núcleo con VectorBT",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

logger = structlog.get_logger(__name__)


def _out() -> Console:
    return Console()


def _err() -> Console:
    return Console(stderr=True)


def _fail(message: str, code: int) -> NoReturn:
    _err().print(message, style="red", markup=False, highlight=False)
    raise typer.Exit(code=code)


def _exit_code(exc: Exception) -> int:
    """Traducción de errores de dominio a los códigos de salida de RF-9 / RF-14.

    Los :class:`BacktestError` llevan su propio ``exit_code`` (1 no encontrado, 2 validación,
    3 ``FAILED``). ``SourceNotFoundError`` (dataset ausente) es un 1; la validación de forma
    (``pydantic``, costes, rango, ``invalid_source``) un 2; el IO un 4. Cualquier otra
    excepción inesperada del motor deja la tarea ``FAILED`` (exit 3, RF-14), nunca un 1 mudo.
    """
    if isinstance(exc, BacktestError):
        return exc.exit_code
    if isinstance(exc, SourceNotFoundError):  # dataset/corrida no encontrada (RF-14)
        return EXIT_GENERIC
    if isinstance(exc, ValueError):  # validación (pydantic, costes, rango, invalid_source)
        return EXIT_VALIDATION
    if isinstance(exc, OSError):
        return EXIT_IO
    return EXIT_FAILED  # excepción inesperada del motor → tarea FAILED (RF-14)


_LABELS = {
    EXIT_GENERIC: "Error",
    EXIT_VALIDATION: "Error de validación",
    EXIT_FAILED: "Tarea FAILED",
    EXIT_IO: "Error de IO/permisos",
}


def _handle(exc: Exception) -> NoReturn:
    code = _exit_code(exc)
    if code in (EXIT_GENERIC, EXIT_FAILED) and not isinstance(exc, LookupError | BacktestError):
        logger.exception("command_failed", error=str(exc))
    _fail(f"{_LABELS[code]}: {exc}", code)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Nivel WARNING"),
    log_format: LogFormatChoice = typer.Option(
        LogFormatChoice.RICH, "--log-format", help="rich | json", case_sensitive=False
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.TABLE, "--format", help="table | json", case_sensitive=False
    ),
) -> None:
    """ft_vectorbt: backtesting de estrategias del núcleo con VectorBT."""
    fmt: LogFormat = "json" if log_format is LogFormatChoice.JSON else "rich"
    configure_logging(
        "cli", level_from_flags(verbose=verbose, quiet=quiet), fmt, component=COMPONENT
    )
    ctx.obj = {"log_format": fmt, "format": output_format.value, "quiet": quiet}


def _format(ctx: typer.Context) -> str:
    return ctx.obj["format"] if ctx.obj is not None else OutputFormat.TABLE.value


# ---------------------------------------------------------------- run


def _build_request(  # noqa: PLR0913 - refleja el cuerpo de BacktestRequest (RF-6)
    *,
    dataset: str | None,
    strategy_id: str | None,
    strategy_version: int | None,
    start_ms: int | None,
    end_ms: int | None,
    initial_cash: float | None,
    size_fraction: float | None,
    fees_bps: float | None,
    slippage_bps: float | None,
    force: bool,
    request_file: Path | None,
) -> BacktestRequest:
    """Petición canónica desde ``--request-file`` **o** las opciones explícitas (RF-6).

    Raises:
        ValueError: SI se combinan ambas formas o faltan ``--dataset``/``--strategy-id``.
        OSError: SI ``--request-file`` no se puede leer.
    """
    explicit = (
        any(
            value is not None
            for value in (
                dataset,
                strategy_id,
                strategy_version,
                start_ms,
                end_ms,
                initial_cash,
                size_fraction,
                fees_bps,
                slippage_bps,
            )
        )
        or force
    )
    if request_file is not None:
        if explicit:
            msg = "--request-file no se combina con las opciones explícitas de run"
            raise ValueError(msg)
        return BacktestRequest.model_validate_json(request_file.read_text(encoding="utf-8"))
    if dataset is None or strategy_id is None:
        msg = "Indique --dataset y --strategy-id (o --request-file)"
        raise ValueError(msg)
    body: dict[str, object] = {
        "dataset": BacktestDataset.parse(dataset).model_dump(),
        "strategy_id": strategy_id,
        "force": force,
    }
    optional = {
        "strategy_version": strategy_version,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "initial_cash": initial_cash,
        "size_fraction": size_fraction,
        "fees_bps": fees_bps,
        "slippage_bps": slippage_bps,
    }
    body.update({key: value for key, value in optional.items() if value is not None})
    return BacktestRequest.model_validate(body)


def _run_with_progress(
    request: BacktestRequest, settings: FinancesSettings, *, show_progress: bool
) -> BacktestTaskResult:
    """Ejecuta la corrida en el hilo actual con barra Rich por fase (en *stderr*)."""
    if not show_progress:
        return service.run_sync(request, settings)
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_err(),
    ) as progress:
        bar = progress.add_task("PENDING", total=_RUN_PHASES)

        def on_phase(phase: str) -> None:
            progress.update(bar, description=phase, advance=1)

        return service.run_sync(request, settings, on_phase)


def _print_result(result: BacktestTaskResult, fmt: str) -> None:
    if fmt == OutputFormat.JSON.value:
        typer.echo(result.model_dump_json(indent=2))
        return
    summary = result.summary
    table = Table(title="Resultado de ft_vectorbt run")
    table.add_column("Métrica", style="cyan")
    table.add_column("Valor", style="green", overflow="fold")
    table.add_row("run_id", result.run_id)
    table.add_row("cached", str(result.cached))
    table.add_row("engine", f"{result.manifest.engine} {result.manifest.engine_version}")
    table.add_row("n_bars", str(summary.n_bars))
    table.add_row("n_trades", str(summary.n_trades))
    table.add_row("total_return", f"{summary.total_return:.6f}")
    table.add_row("max_drawdown", f"{summary.max_drawdown:.6f}")
    table.add_row("final_equity", f"{summary.final_equity:.6f}")
    _out().print(table)


@app.command()
def run(  # noqa: PLR0913 - todas las opciones de RF-6
    ctx: typer.Context,
    dataset: str | None = typer.Option(
        None, "--dataset", "-d", help="Dataset exchange/market_type/symbol/timeframe"
    ),
    strategy_id: str | None = typer.Option(None, "--strategy-id", help="UUID v7 de la estrategia"),
    strategy_version: int | None = typer.Option(
        None, "--strategy-version", help="Versión de la estrategia (por defecto, la última)"
    ),
    start_ms: int | None = typer.Option(None, "--start-ms", help="Inicio del rango (ms UTC)"),
    end_ms: int | None = typer.Option(None, "--end-ms", help="Fin inclusivo del rango (ms UTC)"),
    initial_cash: float | None = typer.Option(None, "--initial-cash", help="Caja inicial"),
    size_fraction: float | None = typer.Option(
        None, "--size-fraction", help="Fracción de la caja por operación (0 < f <= 1)"
    ),
    fees_bps: float | None = typer.Option(None, "--fees-bps", help="Comisión en puntos básicos"),
    slippage_bps: float | None = typer.Option(
        None, "--slippage-bps", help="Deslizamiento en puntos básicos"
    ),
    force: bool = typer.Option(False, "--force", help="Recalcula aunque exista el resultado"),
    request_file: Path | None = typer.Option(
        None, "--request-file", help="JSON con exactamente el cuerpo de POST /backtest/vectorbt/run"
    ),
) -> None:
    """Ejecuta un backtest síncrono con VectorBT (RF-4); ``cached`` sale 0 sin simular."""
    settings = FinancesSettings()
    try:
        request = _build_request(
            dataset=dataset,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            start_ms=start_ms,
            end_ms=end_ms,
            initial_cash=initial_cash,
            size_fraction=size_fraction,
            fees_bps=fees_bps,
            slippage_bps=slippage_bps,
            force=force,
            request_file=request_file,
        )
        show_progress = ctx.obj is None or ctx.obj.get("log_format") == "rich"
        result = _run_with_progress(request, settings, show_progress=show_progress)
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)
    _print_result(result, _format(ctx))


# ---------------------------------------------------------------- kinds


def _print_kinds(infos: list[KindInfo], fmt: str) -> None:
    if fmt == OutputFormat.JSON.value:
        typer.echo(
            "[\n"
            + ",\n".join(info.model_dump_json(indent=2) for info in infos)
            + ("\n]" if infos else "]")
        )
        return
    table = Table(title="Estrategias registradas (ft_vectorbt kinds)")
    for column in ("kind", "version", "origin", "valid", "code_hash", "error"):
        table.add_column(column, overflow="fold")
    for info in infos:
        table.add_row(
            info.kind,
            info.version or "-",
            info.origin,
            str(info.valid),
            (info.code_hash[:12] if info.code_hash else "-"),
            info.error or "-",
        )
    _out().print(table)


@app.command()
def kinds(
    ctx: typer.Context,
    kind: str | None = typer.Option(None, "--kind", help="Filtrar por un único kind"),
    check: bool = typer.Option(
        False, "--check", help="Ejecuta el contrato de autor sobre las locales (solo CLI, RF-20)"
    ),
) -> None:
    """Lista las estrategias registradas (mismo JSON que ``GET /backtest/vectorbt/kinds``)."""
    settings = FinancesSettings()
    try:
        infos = service.kinds(settings)
    except Exception as exc:
        _handle(exc)
    if kind is not None:
        infos = [info for info in infos if info.kind == kind]
    if check:
        _check_strategies(infos, settings)
        return
    _print_kinds(infos, _format(ctx))


def _check_strategies(infos: list[KindInfo], settings: FinancesSettings) -> None:
    """Ejecuta ``assert_strategy_contract`` sobre las locales; sale 2 si alguna falla (RF-6)."""
    local = [info.kind for info in infos if info.origin == "local"]
    err = _err()
    failed: list[str] = []
    for name in local:
        try:
            service.check_strategies(name, settings)
        except Exception as exc:  # noqa: BLE001 - se informa cada fallo y se agrega
            failed.append(name)
            err.print(f"✗ {name}: {exc}", style="red", markup=False, highlight=False)
    if failed:
        _fail(
            f"Error de validación: {len(failed)} estrategia(s) incumplen el contrato",
            EXIT_VALIDATION,
        )
    _out().print(f"OK: {len(local)} estrategia(s) local(es) cumplen el contrato")


# ---------------------------------------------------------------- runs / show (RF-6)


def _print_runs(page: RunsPage, fmt: str) -> None:
    if fmt == OutputFormat.JSON.value:
        typer.echo(page.model_dump_json(indent=2))
        return
    table = Table(title=f"Corridas almacenadas (ft_vectorbt runs) — {page.count}")
    for column in ("run_id", "kind", "strategy_id", "dataset", "n_trades", "created_at_ms"):
        table.add_column(column, overflow="fold")
    for item in page.items:
        table.add_row(
            item.run_id[:12],
            item.kind,
            item.strategy_id,
            item.dataset.path_segment(),
            str(item.summary.n_trades),
            str(item.created_at_ms),
        )
    _out().print(table)
    _err().print(f"next_cursor: {page.next_cursor or '-'}", highlight=False)


@app.command()
def runs(  # noqa: PLR0913 - filtros de RF-6 con nombre
    ctx: typer.Context,
    exchange: str | None = typer.Option(None, "--exchange", help="Filtrar por exchange"),
    market_type: str | None = typer.Option(None, "--market-type", help="Filtrar por market_type"),
    symbol: str | None = typer.Option(None, "--symbol", help="Filtrar por symbol"),
    timeframe: str | None = typer.Option(None, "--timeframe", help="Filtrar por timeframe"),
    strategy_id: str | None = typer.Option(None, "--strategy-id", help="Filtrar por estrategia"),
    limit: int = typer.Option(100, "--limit", help="Tamaño de página (1..10000)"),
    cursor: str | None = typer.Option(None, "--cursor", help="Cursor de la página siguiente"),
) -> None:
    """Lista corridas almacenadas (mismo JSON que ``GET /backtest/vectorbt/runs``)."""
    settings = FinancesSettings()
    try:
        page = service.list_runs(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            cursor=cursor,
            limit=limit,
            settings=settings,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)
    _print_runs(page, _format(ctx))


def _print_show(stored: StoredRun, fmt: str) -> None:
    if fmt == OutputFormat.JSON.value:
        payload = {
            "manifest": json.loads(stored.manifest.model_dump_json()),
            "summary": json.loads(stored.summary.model_dump_json()),
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    manifest, summary = stored.manifest, stored.summary
    table = Table(title="Corrida de ft_vectorbt (show)")
    table.add_column("Campo", style="cyan")
    table.add_column("Valor", style="green", overflow="fold")
    table.add_row("run_id", manifest.run_id)
    table.add_row("engine", f"{manifest.engine} {manifest.engine_version}")
    table.add_row("kind", manifest.strategy.kind)
    table.add_row("strategy_id", manifest.strategy.strategy_id)
    table.add_row("dataset", manifest.request.dataset.path_segment())
    table.add_row("n_bars", str(manifest.n_bars))
    table.add_row("n_trades", str(summary.n_trades))
    table.add_row("total_return", f"{summary.total_return:.6f}")
    table.add_row("max_drawdown", f"{summary.max_drawdown:.6f}")
    table.add_row("final_equity", f"{summary.final_equity:.6f}")
    _out().print(table)


@app.command()
def show(
    ctx: typer.Context, run_id: str = typer.Argument(..., help="run_id de la corrida")
) -> None:
    """Manifiesto y resumen de una corrida (``GET /backtest/vectorbt/runs/{run_id}``)."""
    settings = FinancesSettings()
    try:
        stored = service.find_run(run_id, settings)
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)
    if stored is None:
        _fail(f"Error: corrida no encontrada: {run_id}", EXIT_GENERIC)
    _print_show(stored, _format(ctx))


# ---------------------------------------------------------------- trades / equity (RF-6)


def _print_page(
    items: list[dict[str, Any]], count: int, next_after_ms: int | None, title: str, fmt: str
) -> None:
    table = Table(title=f"{title} — {count}")
    columns = list(items[0].keys()) if items else []
    for column in columns:
        table.add_column(column, overflow="fold")
    for item in items:
        table.add_row(*(str(item[column]) for column in columns))
    _out().print(table)
    _err().print(f"next_after_ms: {next_after_ms if next_after_ms is not None else '-'}")


@app.command()
def trades(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="run_id de la corrida"),
    limit: int = typer.Option(1000, "--limit", help="Tamaño de página (1..10000)"),
    after_ms: int | None = typer.Option(
        None, "--after-ms", help="Cursor exclusivo (entry_time_ms)"
    ),
) -> None:
    """Operaciones paginadas de una corrida (``GET /runs/{run_id}/trades``)."""
    settings = FinancesSettings()
    try:
        page: TradesPage | None = service.read_trades(
            run_id, after_ms=after_ms, limit=limit, settings=settings
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)
    if page is None:
        _fail(f"Error: corrida no encontrada: {run_id}", EXIT_GENERIC)
    if _format(ctx) == OutputFormat.JSON.value:
        typer.echo(page.model_dump_json(indent=2))
        return
    _print_page(page.items, page.count, page.next_after_ms, "Operaciones (trades)", _format(ctx))


@app.command()
def equity(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="run_id de la corrida"),
    limit: int = typer.Option(1000, "--limit", help="Tamaño de página (1..10000)"),
    after_ms: int | None = typer.Option(None, "--after-ms", help="Cursor exclusivo (open_time_ms)"),
) -> None:
    """Curva de capital paginada de una corrida (``GET /runs/{run_id}/equity``)."""
    settings = FinancesSettings()
    try:
        page: EquityPage | None = service.read_equity(
            run_id, after_ms=after_ms, limit=limit, settings=settings
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)
    if page is None:
        _fail(f"Error: corrida no encontrada: {run_id}", EXIT_GENERIC)
    if _format(ctx) == OutputFormat.JSON.value:
        typer.echo(page.model_dump_json(indent=2))
        return
    _print_page(
        page.items, page.count, page.next_after_ms, "Curva de capital (equity)", _format(ctx)
    )


# ---------------------------------------------------------------- status / result (RF-6)


def _print_status(task_id: str, row: dict[str, Any], fmt: str) -> None:
    view: dict[str, Any] = {
        "task_id": task_id,
        "status": row.get("status"),
        "phase": row.get("phase"),
        "error": row.get("error"),
        "summary": json.loads(row["result_summary_json"]) if row.get("result_summary_json") else {},
    }
    if fmt == OutputFormat.JSON.value:
        typer.echo(json.dumps(view, indent=2))
        return
    table = Table(title="Estado de la tarea (ft_vectorbt status)")
    table.add_column("Campo", style="cyan")
    table.add_column("Valor", style="green", overflow="fold")
    table.add_row("task_id", task_id)
    table.add_row("status", str(view["status"]))
    table.add_row("phase", str(view["phase"]))
    table.add_row("error", str(view["error"] or "-"))
    _out().print(table)


@app.command()
def status(
    ctx: typer.Context, task_id: str = typer.Argument(..., help="task_id de la tarea de la API")
) -> None:
    """Estado y fase de una tarea despachada (``GET /backtest/vectorbt/status/{task_id}``)."""
    settings = FinancesSettings()
    try:
        row = service.task_status(task_id, settings)
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)
    if row is None:
        _fail(f"Error: tarea no encontrada: {task_id}", EXIT_GENERIC)
    _print_status(task_id, row, _format(ctx))


@app.command()
def result(
    ctx: typer.Context, task_id: str = typer.Argument(..., help="task_id de la tarea de la API")
) -> None:
    """Resultado de una tarea terminada (``GET /backtest/vectorbt/result/{task_id}``).

    No encontrada → exit 1; aún sin terminar → exit 1; ``FAILED`` → exit 3 (RF-9).
    """
    settings = FinancesSettings()
    try:
        task_result = service.task_result(task_id, settings)
    except service.TaskFailedError as exc:
        _fail(f"Tarea FAILED: {exc}", EXIT_FAILED)
    except TaskNotFinishedError as exc:
        _fail(f"Error: la tarea aún no ha terminado: {exc.status}", EXIT_GENERIC)
    except typer.Exit:
        raise
    except Exception as exc:
        _handle(exc)
    if task_result is None:
        _fail(f"Error: tarea no encontrada: {task_id}", EXIT_GENERIC)
    _print_result(task_result, _format(ctx))


if __name__ == "__main__":
    app()
