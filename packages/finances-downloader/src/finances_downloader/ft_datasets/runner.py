"""Ejecución de las CLI públicas de los descargadores como subprocesos (plan §3.7, ADR-D01).

``ft_datasets`` nunca importa a los descargadores (Art. 4): su única referencia son los
nombres de sus *console scripts*. Este módulo construye el ``argv`` exacto, resuelve el
ejecutable (``sysconfig`` *scripts* del mismo entorno → ``PATH``), prepara el entorno del
hijo para que escriba exactamente donde el padre lee y ejecuta cada bloque con *timeout*,
conservando las últimas 50 líneas de ``stderr`` y el ``task_id`` del descargador.

:class:`Runner` es un ``Protocol`` inyectable: los tests usan un *runner* falso que
escribe Parquet sintético (sin red ni subprocesos de descarga reales).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import structlog
from finances_core.config import FinancesSettings
from finances_core.schemas.datasets import BlockRun, Exchange

logger = structlog.get_logger(__name__)

#: *Console scripts* públicos de cada descargador (única referencia a ellos, Art. 4).
DOWNLOADER_SCRIPTS: dict[str, str] = {
    "binance": "ft_market_data_binance",
    "yfinance": "ft_market_data_yfinance",
}
#: Flags globales (antes del subcomando): logs JSON y solo WARNING+ en ``stderr``.
GLOBAL_FLAGS: tuple[str, ...] = ("--log-format", "json", "-q")
DOWNLOAD_COMMAND = "download"
#: Flags de ``download`` que usa ``ft_datasets`` (fijados por el test de contrato T3.1).
DOWNLOAD_FLAGS: dict[str, tuple[str, ...]] = {
    "binance": ("--symbol", "--timeframe", "--start", "--end", "--market"),
    "yfinance": ("--symbol", "--timeframe", "--start", "--end"),
}
STDERR_TAIL_LINES = 50
_KILL_GRACE_S = 10.0
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: Resuelve el nombre de un *console script* a su comando (``None`` = no encontrado).
CommandResolver = Callable[[str], list[str] | None]


def build_argv(
    exchange: Exchange,
    market_type: str,
    symbols: Sequence[str],
    timeframes: Sequence[str],
    start_ms: int,
    end_ms_inclusive: int,
) -> list[str]:
    """Comando exacto de un bloque (``argv[0]`` = nombre del *console script*, sin ruta)."""
    argv = [DOWNLOADER_SCRIPTS[exchange], *GLOBAL_FLAGS, DOWNLOAD_COMMAND]
    for symbol in symbols:
        argv += ["--symbol", symbol]
    for timeframe in timeframes:
        argv += ["--timeframe", timeframe]
    argv += ["--start", str(start_ms), "--end", str(end_ms_inclusive)]
    if exchange == "binance":
        argv += ["--market", market_type]
    return argv


def _candidates(directory: Path, name: str) -> list[Path]:
    names = [name]
    if sys.platform == "win32":
        names = [f"{name}.exe", f"{name}.cmd", f"{name}.bat", name]
    return [directory / n for n in names]


def resolve_executable(name: str, search_dirs: Sequence[Path] | None = None) -> str | None:
    """Ruta del ejecutable ``name``: primero en ``search_dirs`` (por defecto, el directorio
    de *scripts* del entorno actual), luego en ``PATH``; ``None`` si no existe."""
    dirs = (
        list(search_dirs)
        if search_dirs is not None
        else [Path(sysconfig.get_path("scripts")), Path(sys.executable).parent]
    )
    for directory in dirs:
        for candidate in _candidates(directory, name):
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name)


def default_resolver(name: str) -> list[str] | None:
    """:data:`CommandResolver` por defecto (:func:`resolve_executable`)."""
    path = resolve_executable(name)
    return [path] if path else None


def child_env(settings: FinancesSettings, datasets_dir: Path) -> dict[str, str]:
    """Entorno del hijo: ``os.environ`` + rutas absolutas para escribir donde el padre lee."""
    env = dict(os.environ)
    env["FT_PROJECT_ROOT"] = str(settings.PROJECT_ROOT.resolve())
    env["FT_PARQUET_DIR"] = str(datasets_dir.resolve())
    env["FT_TASKS_DB_PATH"] = str(settings.TASKS_DB_PATH.resolve())
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def tail_lines(text: str, limit: int = STDERR_TAIL_LINES) -> list[str]:
    """Últimas ``limit`` líneas no vacías de ``text``."""
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def extract_task_id(stderr: str, stdout: str = "") -> str | None:
    """Primer ``task_id`` de los logs JSON de ``stderr``; si no hay, el de la fila
    ``Task ID`` de la tabla de resultado en ``stdout``."""
    for line in stderr.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        task_id = event.get("task_id") if isinstance(event, dict) else None
        if isinstance(task_id, str) and _UUID_RE.fullmatch(task_id):
            return task_id
    for line in stdout.splitlines():
        if "Task ID" in line:
            match = _UUID_RE.search(line)
            if match:
                return match.group(0)
    return None


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


class Runner(Protocol):
    """Ejecuta un bloque de descarga (síncrono; el servicio lo corre en un hilo)."""

    def run_block(
        self, block: int, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> BlockRun: ...


class SubprocessRunner:
    """:class:`Runner` real: lanza la CLI del descargador como subproceso."""

    def __init__(self, resolver: CommandResolver | None = None) -> None:
        self.resolver = resolver or default_resolver

    def run_block(
        self, block: int, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> BlockRun:
        """Ejecuta ``argv`` con *timeout*; nunca lanza (el fallo queda en el ``BlockRun``)."""
        started = time.perf_counter()

        def done(exit_code: int | None, timed_out: bool, stderr: str, stdout: str) -> BlockRun:
            return BlockRun(
                block=block,
                exit_code=exit_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
                timed_out=timed_out,
                stderr_tail=tail_lines(stderr),
                downloader_task_id=extract_task_id(stderr, stdout),
            )

        name = argv[0]
        command = self.resolver(name)
        if command is None:
            message = f"Ejecutable del descargador no encontrado: {name}"
            logger.error("downloader_not_found", executable=name)
            return done(None, False, message, "")
        full = [*command, *argv[1:]]
        logger.info("downloader_block_start", block=block, argv=list(argv))
        try:
            proc = subprocess.Popen(
                full,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            message = f"No se pudo lanzar {name}: {exc}"
            logger.exception("downloader_launch_failed", executable=name, error=str(exc))
            return done(None, False, message, "")
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            stderr = _decode(err) + f"\nTimeout: {name} superó {timeout_s:g} s y fue terminado"
            logger.exception("downloader_block_timeout", block=block, timeout_s=timeout_s)
            return done(None, True, stderr, _decode(out))
        run = done(proc.returncode, False, _decode(err), _decode(out))
        logger.info(
            "downloader_block_end",
            block=block,
            exit_code=run.exit_code,
            duration_ms=run.duration_ms,
            downloader_task_id=run.downloader_task_id,
        )
        return run
