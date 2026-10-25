"""Comando ``ft_serve``: levanta el servicio HTTP único (RF-2)."""

from __future__ import annotations

import logging

import typer
from finances_core.config import FinancesSettings

from finances_api.app import create_app

app = typer.Typer(name="ft_serve", add_completion=False, help="Servicio HTTP de financial_tools")

EXIT_MISSING_DEPENDENCY = 3


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host", help="Host (default: FT_API_HOST)"),
    port: int | None = typer.Option(None, "--port", help="Puerto (default: FT_API_PORT)"),
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG | INFO | WARNING | ERROR"),
) -> None:
    """Levanta FastAPI con los routers de todas las herramientas."""
    try:
        import uvicorn  # noqa: PLC0415 - dependencia opcional del extra 'service'
    except ImportError as exc:
        typer.echo(
            "uvicorn no está instalado. Instalarlo con: pip install 'finances-api[service]'",
            err=True,
        )
        raise typer.Exit(code=EXIT_MISSING_DEPENDENCY) from exc

    settings = FinancesSettings()
    level = getattr(logging, log_level.upper(), logging.INFO)
    uvicorn.run(
        create_app(level),
        host=host or settings.API_HOST,
        port=port or settings.API_PORT,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    app()
