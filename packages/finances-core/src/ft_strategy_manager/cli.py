"""CLI de ``ft_strategy_manager`` (Typer): adaptador delgado sobre ``service`` (Art. 1).

Los datos salen por stdout y los logs/errores por stderr, de modo que ``--format json``
se puede canalizar. Códigos de salida (T4.5): 0 OK, 1 no encontrado, 2 validación,
3 conflicto (nombre duplicado), 4 error de E/S.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

import typer
import yaml
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from finances_core.logging import LogFormat
from finances_core.schemas.strategy import (
    DiffResult,
    Strategy,
    StrategyCreate,
    StrategyListFilters,
    StrategyListResponse,
    StrategyUpdate,
    StrategyVersion,
)
from ft_strategy_manager import service
from ft_strategy_manager.errors import (
    NoChangesError,
    StrategyConflictError,
    StrategyNotFoundError,
    StrategyValidationError,
    VersionNotFoundError,
)
from ft_strategy_manager.logging import configure

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_VALIDATION = 2
EXIT_CONFLICT = 3
EXIT_IO = 4

_VERSION_ARG = re.compile(r"^[vV]?(\d+)$")

app = typer.Typer(
    name="ft_strategy_manager",
    help="Gestión de estrategias: CRUD, versionado, diff, auditoría",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

TableOrJson = Literal["table", "json"]
TableJsonYaml = Literal["table", "json", "yaml"]


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo WARNING+"),
    log_format: LogFormat = typer.Option("rich", "--log-format", help="rich | json"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
    configure("cli", verbose=verbose, quiet=quiet, log_format=log_format)
    ctx.obj = {"verbose": verbose, "quiet": quiet, "log_format": log_format}


# ------------------------------------------------------------------------- helpers
def _fail(message: str, code: int) -> NoReturn:
    err_console.print(f"[red]{message}[/red]", markup=True, highlight=False)
    raise typer.Exit(code=code)


@contextmanager
def _handle_errors() -> Iterator[None]:
    """Traduce las excepciones de negocio a mensajes y códigos de salida (RF → T4.5)."""
    try:
        yield
    except (StrategyNotFoundError, VersionNotFoundError) as exc:
        _fail(_escape(str(exc)), EXIT_NOT_FOUND)
    except StrategyConflictError as exc:
        _fail(_escape(str(exc)), EXIT_CONFLICT)
    except (StrategyValidationError, NoChangesError) as exc:
        _fail(_escape(str(exc)), EXIT_VALIDATION)
    except ValidationError as exc:
        _fail(_escape(_pydantic_message(exc)), EXIT_VALIDATION)
    except (OSError, sqlite3.Error) as exc:
        _fail(_escape(f"I/O error: {exc}"), EXIT_IO)


def _escape(text: str) -> str:
    return text.replace("[", "\\[")


def _pydantic_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'payload'}: {e['msg']}" for e in exc.errors()
    )


def _read_json_file(path: Path, kind: type) -> Any:
    """Lee un archivo JSON (T4.4): ilegible → E/S (4); contenido inválido → validación (2)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(_escape(f"Cannot read {path}: {exc.strerror or exc}"), EXIT_IO)
    except UnicodeDecodeError as exc:
        _fail(_escape(f"{path} is not valid UTF-8 text: {exc.reason}"), EXIT_VALIDATION)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _fail(_escape(f"{path} is not valid JSON: {exc}"), EXIT_VALIDATION)
    if not isinstance(data, kind):
        _fail(
            _escape(f"{path} must contain a JSON {'object' if kind is dict else 'array'}"),
            EXIT_VALIDATION,
        )
    return data


def _parse_version(value: str, option: str) -> int:
    match = _VERSION_ARG.match(value.strip())
    if match is None or int(match.group(1)) < 1:
        _fail(_escape(f"{option} must be a version like 3 or v3 (got {value!r})"), EXIT_VALIDATION)
    return int(match.group(1))


def _fmt_ms(value: int | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)


def _emit(model: BaseModel, fmt: TableJsonYaml) -> bool:
    """Imprime ``model`` en json/yaml y devuelve ``True``; ``False`` si fmt es ``table``."""
    if fmt == "json":
        typer.echo(model.model_dump_json(indent=2))
        return True
    if fmt == "yaml":
        typer.echo(
            yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
            nl=False,
        )
        return True
    return False


def _version_table(version: StrategyVersion, title: str) -> Table:
    table = Table(title=title)
    table.add_column("Campo", style="cyan")
    table.add_column("Valor", overflow="fold")
    table.add_row("strategy_id", version.strategy_id)
    table.add_row("version", str(version.version))
    table.add_row("hash", version.hash)
    table.add_row("created_at", _fmt_ms(version.created_at_ms))
    table.add_row("created_by", version.created_by or "-")
    return table


# --------------------------------------------------------------------------- comandos
@app.command()
def create(
    name: str = typer.Argument(..., help="Nombre único (snake_case, 3-64 chars)"),
    parameters_file: Path = typer.Option(..., "--params", "-p", help="Archivo JSON con parámetros"),
    description: str | None = typer.Option(None, "--desc", "-d", help="Descripción (máx. 500)"),
    schema_file: Path | None = typer.Option(
        None, "--schema", "-s", help="Archivo JSON Schema (opcional)"
    ),
    tags: list[str] | None = typer.Option(None, "--tag", "-t", help="Tags (repetible)"),
    user: str | None = typer.Option(None, "--user", help="Autor para auditoría (X-User-ID)"),
    format: TableOrJson = typer.Option("table", "--format", "-f", help="table | json"),
) -> None:
    """Crea nueva estrategia v1."""
    with _handle_errors():
        parameters = _read_json_file(parameters_file, dict)
        schema = _read_json_file(schema_file, dict) if schema_file is not None else None
        payload = StrategyCreate(
            name=name,
            description=description,
            parameters=parameters,
            parameter_schema=schema,
            tags=tags or [],
        )
        created = service.create_strategy(payload, user=user)
    if not _emit(created, format):
        console.print(_version_table(created, f"Estrategia creada: {name}"))


@app.command()
def get(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    version: int | None = typer.Option(
        None, "--version", "-v", min=1, help="Versión específica (default: latest)"
    ),
    format: TableJsonYaml = typer.Option("table", "--format", "-f", help="table | json | yaml"),
) -> None:
    """Obtiene estrategia completa con parámetros."""
    with _handle_errors():
        strategy = service.get_strategy(strategy_id, version)
    if not _emit(strategy, format):
        _print_strategy(strategy)


def _print_strategy(strategy: Strategy) -> None:
    selected = strategy.selected
    head = Table(title=f"Estrategia {strategy.name}")
    head.add_column("Campo", style="cyan")
    head.add_column("Valor", overflow="fold")
    head.add_row("id", strategy.id)
    head.add_row("name", strategy.name)
    head.add_row("description", strategy.description or "-")
    head.add_row("tags", ", ".join(strategy.tags) or "-")
    head.add_row("created_at", _fmt_ms(strategy.created_at_ms))
    head.add_row("deleted_at", _fmt_ms(strategy.deleted_at_ms))
    head.add_row("versiones", ", ".join(str(v.version) for v in strategy.versions))
    if selected is not None:
        head.add_row("versión mostrada", str(selected.version))
        head.add_row("hash", selected.hash)
    console.print(head)
    if selected is not None:
        console.print(Syntax(_json_text(selected.parameters), "json", word_wrap=True))


@app.command()
def update(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    parameters_file: Path | None = typer.Option(
        None, "--params", "-p", help="JSON completo (reemplazo)"
    ),
    patch_file: Path | None = typer.Option(None, "--patch", help="JSON Patch RFC 6902 (parcial)"),
    description: str | None = typer.Option(None, "--desc", "-d", help="Nueva descripción"),
    tags: list[str] | None = typer.Option(None, "--tag", "-t", help="Reemplaza tags (repetible)"),
    user: str | None = typer.Option(None, "--user", help="Autor para auditoría (X-User-ID)"),
    format: TableOrJson = typer.Option("table", "--format", "-f", help="table | json"),
) -> None:
    """Crea nueva versión de estrategia existente."""
    with _handle_errors():
        parameters = _read_json_file(parameters_file, dict) if parameters_file else None
        patch = _read_json_file(patch_file, list) if patch_file else None
        payload = StrategyUpdate(
            parameters=parameters,
            parameters_patch=patch,
            description=description,
            tags=tags,
        )
        created = service.update_strategy(strategy_id, payload, user=user)
    if not _emit(created, format):
        console.print(_version_table(created, f"Nueva versión v{created.version}"))


@app.command()
def delete(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    purge: bool = typer.Option(False, "--purge", help="Hard delete irreversible (confirma)"),
    force: bool = typer.Option(False, "--force", "-f", help="Saltar confirmación en --purge"),
    user: str | None = typer.Option(None, "--user", help="Autor para auditoría (X-User-ID)"),
) -> None:
    """Elimina estrategia (soft-delete por defecto; idempotente)."""
    if purge and not force:
        typer.confirm(
            f"Esto elimina IRREVERSIBLEMENTE {strategy_id} y todas sus versiones. ¿Continuar?",
            abort=True,
        )
    with _handle_errors():
        service.delete_strategy(strategy_id, purge=purge, user=user)
    console.print(f"{'Purgada' if purge else 'Eliminada (soft)'}: {strategy_id}", highlight=False)


@app.command("list")
def list_(
    tag: str | None = typer.Option(None, "--tag", help="Filtrar por tag exacto"),
    name_contains: str | None = typer.Option(None, "--name", help="Substring en nombre"),
    include_deleted: bool = typer.Option(False, "--include-deleted", help="Incluir borradas"),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=500, help="Tamaño de página"),
    offset: int = typer.Option(0, "--offset", "-o", min=0, help="Desplazamiento"),
    format: TableOrJson = typer.Option("table", "--format", "-f", help="table | json"),
) -> None:
    """Lista estrategias con filtros y paginación."""
    with _handle_errors():
        filters = StrategyListFilters(
            tag=tag,
            name_contains=name_contains,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
        )
        response = service.list_strategies(filters)
    if not _emit(response, format):
        console.print(_list_table(response))


def _list_table(response: StrategyListResponse) -> Table:
    table = Table(title="Estrategias")
    for column in ("ID", "Nombre", "Ver.", "Actualizada", "Tags", "Hash"):
        table.add_column(column, style="cyan" if column == "Nombre" else None, overflow="fold")
    for item in response.items:
        name = f"[strike]{item.name}[/strike]" if item.deleted_at_ms is not None else item.name
        table.add_row(
            item.id,
            name,
            str(item.latest_version),
            _fmt_ms(item.updated_at_ms),
            ", ".join(item.tags) or "-",
            item.hash_latest[:12],
        )
    shown = len(response.items)
    table.caption = (
        f"{shown} de {response.total_count} (limit={response.limit}, offset={response.offset})"
    )
    return table


@app.command()
def diff(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    from_version: str = typer.Option(..., "--from", help="Versión origen (3 o v3)"),
    to_version: str = typer.Option(..., "--to", help="Versión destino (3 o v3)"),
    format: TableJsonYaml = typer.Option("table", "--format", "-f", help="table | json | yaml"),
) -> None:
    """Diff semántico entre dos versiones."""
    with _handle_errors():
        result = service.diff_versions(
            strategy_id,
            _parse_version(from_version, "--from"),
            _parse_version(to_version, "--to"),
        )
    if not _emit(result, format):
        console.print(_diff_table(result))


def _diff_table(result: DiffResult) -> Table:
    table = Table(title=f"Diff v{result.from_version} → v{result.to_version}")
    table.add_column("Cambio")
    table.add_column("Clave", style="cyan")
    table.add_column("Antes", overflow="fold")
    table.add_column("Después", overflow="fold")
    for key, value in sorted(result.added.items()):
        table.add_row("[green]añadido[/green]", key, "-", json.dumps(value, ensure_ascii=False))
    for key, value in sorted(result.removed.items()):
        table.add_row("[red]eliminado[/red]", key, json.dumps(value, ensure_ascii=False), "-")
    for key, (old, new) in sorted(result.changed.items()):
        table.add_row(
            "[yellow]cambiado[/yellow]",
            key,
            json.dumps(old, ensure_ascii=False),
            json.dumps(new, ensure_ascii=False),
        )
    if not (result.added or result.removed or result.changed):
        table.caption = "Sin diferencias"
    return table


@app.command()
def versions(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=500, help="Tamaño de página"),
    offset: int = typer.Option(0, "--offset", "-o", min=0, help="Desplazamiento"),
    format: TableOrJson = typer.Option("table", "--format", "-f", help="table | json"),
) -> None:
    """Lista todas las versiones de una estrategia."""
    with _handle_errors():
        items = service.list_versions(strategy_id, limit, offset)
    if format == "json":
        typer.echo(json.dumps([v.model_dump(mode="json") for v in items], indent=2))
        return
    table = Table(title=f"Versiones de {strategy_id}")
    for column in ("Ver.", "Hash", "Creada", "Autor"):
        table.add_column(column, overflow="fold")
    for item in items:
        table.add_row(
            str(item.version), item.hash[:12], _fmt_ms(item.created_at_ms), item.created_by or "-"
        )
    console.print(table)


@app.command()
def export(
    strategy_id: str = typer.Argument(..., help="UUID v7 de la estrategia"),
    format: Literal["json", "yaml"] = typer.Option("json", "--format", "-f", help="json | yaml"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Archivo de salida (def: stdout)"
    ),
) -> None:
    """Exporta la estrategia con todas sus versiones a JSON/YAML."""
    with _handle_errors():
        content = service.export_strategy(strategy_id, format)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
    if output is not None:
        console.print(f"Exportado a {output}", highlight=False)
    else:
        typer.echo(content.decode("utf-8"), nl=False)


if __name__ == "__main__":  # pragma: no cover
    app()
