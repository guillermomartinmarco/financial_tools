"""Entrypoint de servicio para ``ft_setup`` (uvicorn + logging JSON)."""

from __future__ import annotations

import logging

import typer

from finances_core.config import settings
from ft_setup.api import configure_api_logging, create_app

app = typer.Typer(add_completion=False, help="Servicio HTTP de ft_setup")


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Host (default: FT_API_HOST)"),
    port: int = typer.Option(None, "--port", help="Puerto (default: FT_API_PORT)"),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Levanta el servicio FastAPI de diagnóstico/inicialización."""
    try:
        import uvicorn  # noqa: PLC0415 - dependencia opcional del extra 'service'
    except ImportError as exc:  # pragma: no cover - dependencia opcional
        typer.echo(
            "uvicorn no está instalado. Instalarlo con: "
            "pip install 'finances-core[service]'",
            err=True,
        )
        raise typer.Exit(code=3) from exc

    configure_api_logging(getattr(logging, log_level.upper(), logging.INFO))
    uvicorn.run(
        create_app(),
        host=host or settings.API_HOST,
        port=port or settings.API_PORT,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    app()
