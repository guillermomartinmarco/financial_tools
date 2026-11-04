"""Gestor canónico de tareas persistidas en SQLite (WAL).

Art. 2 Constitución: el estado de procesamiento pesado se persiste en SQLite con
WAL habilitado. Cada llamada abre su propia conexión, por lo que el gestor es
seguro entre hilos y entre procesos (Art. 16).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from finances_core.config import FinancesSettings

logger = structlog.get_logger(__name__)

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED"})

#: Espera ante bloqueos de otra conexión (``busy_timeout``), en segundos.
_BUSY_TIMEOUT_S = 30.0
#: Pausa entre reintentos al inicializar una base nueva desde varios hilos o procesos;
#: los reintentos terminan a los ``_BUSY_TIMEOUT_S`` en total, como sin reintentos.
_INIT_BACKOFF_S = 0.05

#: Columnas escribibles de la tabla ``tasks``.
_TASK_COLUMNS = (
    "status",
    "phase",
    "created_at_ms",
    "started_at_ms",
    "finished_at_ms",
    "downloaded_bytes",
    "processed_candles",
    "gaps_found",
    "gaps_filled_public",
    "gaps_filled_rest",
    "errors_json",
    "parquet_paths_json",
    "stats_json",
    "result_summary_json",
    "error",
    "current_symbol",
    "current_timeframe",
)

_JSON_COLUMNS = frozenset(
    {"errors_json", "parquet_paths_json", "stats_json", "result_summary_json"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    phase TEXT,
    created_at_ms INTEGER,
    started_at_ms INTEGER,
    finished_at_ms INTEGER,
    downloaded_bytes INTEGER DEFAULT 0,
    processed_candles INTEGER DEFAULT 0,
    gaps_found INTEGER DEFAULT 0,
    gaps_filled_public INTEGER DEFAULT 0,
    gaps_filled_rest INTEGER DEFAULT 0,
    errors_json TEXT DEFAULT '[]',
    parquet_paths_json TEXT DEFAULT '[]',
    stats_json TEXT DEFAULT '{}',
    result_summary_json TEXT DEFAULT '{}',
    error TEXT,
    current_symbol TEXT,
    current_timeframe TEXT
);
"""


def now_ms() -> int:
    """Timestamp actual en milisegundos UTC (int64, Art. 5)."""
    return int(time.time() * 1000)


class SQLTaskManager:
    """Persistencia de tareas en ``share/tasks.db`` con modo WAL."""

    def __init__(self, db_path: Path | None = None) -> None:
        # Settings frescos: la raíz se resuelve en el momento de uso, no al importar.
        self.db_path = Path(db_path) if db_path is not None else FinancesSettings().TASKS_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ infra
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Conexión de una transacción: confirma al salir, revierte ante error y siempre cierra.

        ``with sqlite3.Connection`` solo gestiona la transacción y deja la conexión abierta;
        el cierre es explícito.
        """
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Crea el esquema y fija el modo WAL, que persiste en el archivo.

        Pasar a WAL exige un bloqueo exclusivo y SQLite puede responder ``database is
        locked`` sin esperar el ``busy_timeout`` cuando varios hilos o procesos crean la
        base a la vez: se reintenta hasta un plazo total de ``_BUSY_TIMEOUT_S``.
        """
        deadline = time.monotonic() + _BUSY_TIMEOUT_S
        while True:
            try:
                with self._connect() as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(_SCHEMA)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(_INIT_BACKOFF_S)
            else:
                return

    @staticmethod
    def _encode(key: str, value: Any) -> Any:
        if key in _JSON_COLUMNS and not isinstance(value, str):
            return json.dumps(value, default=str)
        return value

    # ---------------------------------------------------------------- API
    def create_task(
        self,
        task_id: str,
        status: str = "PENDING",
        phase: str | None = None,
        **fields: Any,
    ) -> None:
        """Inserta una tarea. Idempotente: si ya existe no la duplica."""
        values: dict[str, Any] = {
            "status": status,
            "phase": phase or status,
            "created_at_ms": now_ms(),
        }
        values.update({k: v for k, v in fields.items() if k in _TASK_COLUMNS})
        columns = ["task_id", *values.keys()]
        placeholders = ", ".join("?" for _ in columns)
        payload = [task_id, *[self._encode(k, v) for k, v in values.items()]]
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO tasks ({', '.join(columns)}) VALUES ({placeholders})",
                payload,
            )

    def update_task(self, task_id: str, **fields: Any) -> None:
        """Actualiza los campos indicados, con transiciones de tiempo automáticas."""
        updates: dict[str, Any] = {k: v for k, v in fields.items() if k in _TASK_COLUMNS}
        if not updates:
            return

        status = updates.get("status")
        if status is not None:
            updates.setdefault("phase", status)
            if status in TERMINAL_STATUSES:
                updates.setdefault("finished_at_ms", now_ms())
            elif status != "PENDING":
                current = self.get_task(task_id)
                if current is not None and current.get("started_at_ms") is None:
                    updates.setdefault("started_at_ms", now_ms())

        assignments = ", ".join(f"{key} = ?" for key in updates)
        payload = [self._encode(k, v) for k, v in updates.items()] + [task_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE task_id = ?", payload)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at_ms DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_ready(self) -> None:
        """Verifica que la base exista y esté en modo WAL (usado por ``ft_setup init``)."""
        with self._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":  # pragma: no cover - defensivo
            logger.warning("tasks_db_not_wal", path=str(self.db_path), mode=mode)
