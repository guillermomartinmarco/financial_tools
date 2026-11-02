"""Persistencia SQLite WAL de ``ft_strategy_manager`` (RF-8, ADR-S06, Art. 2 y 16).

Sigue el patrón de ``SQLTaskManager``: cada ``get_connection`` abre su propia conexión
(aislada por hilo y por proceso) y la cierra al salir. Las escrituras usan
``BEGIN IMMEDIATE`` para serializar la asignación de números de versión (ADR-S01: las
versiones solo se insertan, nunca se actualizan).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from finances_core.config import FinancesSettings
from finances_core.schemas.strategy import (
    Strategy,
    StrategyListFilters,
    StrategySummary,
    StrategyVersion,
)
from ft_strategy_manager.errors import StrategyConflictError

logger = structlog.get_logger(__name__)

#: Variable de entorno que sobreescribe la ruta de ``strategies.db``.
DB_PATH_ENV = "FT_STRATEGIES_DB_PATH"
DB_FILENAME = "strategies.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    parameter_schema TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    deleted_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    created_by TEXT,
    description TEXT,
    tags TEXT,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
    UNIQUE(strategy_id, version)
);

CREATE INDEX IF NOT EXISTS idx_strategies_name ON strategies(name);
CREATE INDEX IF NOT EXISTS idx_versions_strategy_id ON strategy_versions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_versions_strategy_version
    ON strategy_versions(strategy_id, version);
"""

_VERSION_COLUMNS = (
    "strategy_id, version, parameters_json, hash, created_at_ms, created_by, description, tags"
)


def resolve_db_path() -> Path:
    """Ruta absoluta de ``strategies.db``.

    Prioridad: ``FT_STRATEGIES_DB_PATH`` > ``<SHARE_DIR>/strategies.db``. Las rutas relativas
    se resuelven contra la raíz del proyecto, nunca contra el directorio actual (Art. 2).
    """
    settings = FinancesSettings()
    # Las rutas relativas ya vienen resueltas contra PROJECT_ROOT por FinancesSettings.
    if settings.STRATEGIES_DB_PATH is not None:
        return settings.STRATEGIES_DB_PATH
    return settings.SHARE_DIR / DB_FILENAME


def init_db(db_path: Path) -> None:
    """Crea la base, las tablas y los índices, y habilita WAL. Idempotente."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate_version_snapshot(conn)
    finally:
        conn.close()


def _migrate_version_snapshot(conn: sqlite3.Connection) -> None:
    """Deriva 10: añade ``description``/``tags`` a ``strategy_versions`` de bases anteriores.

    Idempotente y segura ante procesos concurrentes (``BEGIN IMMEDIATE`` y nueva comprobación
    dentro de la transacción). Las versiones previas quedan sin snapshot (``tags`` nulo); la
    última de cada estrategia recibe los metadatos vigentes. No toca parámetros ni hashes.
    """

    def missing() -> bool:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_versions)")}
        return not {"description", "tags"} <= columns

    if not missing():
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if missing():
            conn.execute("ALTER TABLE strategy_versions ADD COLUMN description TEXT")
            conn.execute("ALTER TABLE strategy_versions ADD COLUMN tags TEXT")
            conn.execute(
                "UPDATE strategy_versions SET"
                " description = (SELECT s.description FROM strategies s"
                "                WHERE s.id = strategy_versions.strategy_id),"
                " tags = (SELECT s.tags FROM strategies s"
                "         WHERE s.id = strategy_versions.strategy_id)"
                " WHERE version = (SELECT MAX(v.version) FROM strategy_versions v"
                "                  WHERE v.strategy_id = strategy_versions.strategy_id)"
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def get_connection(db_path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    """Conexión propia (por llamada) con WAL, FKs activas y transacción opcional de escritura.

    Con ``write=True`` abre ``BEGIN IMMEDIATE`` y hace commit al salir (rollback si hay
    excepción). Sin ``write`` no abre transacción: las lecturas ven el último commit.
    """
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        else:
            if conn.in_transaction:
                conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------- helpers
def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    """Ejecuta una sentencia y devuelve las filas afectadas."""
    return conn.execute(sql, params).rowcount


def execute_many(conn: sqlite3.Connection, sql: str, rows: Sequence[Sequence[Any]]) -> int:
    """Ejecuta ``sql`` por cada fila de ``rows`` y devuelve las filas afectadas."""
    return conn.executemany(sql, rows).rowcount


def fetch_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(sql, params).fetchone()
    return row


def fetch_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


# ----------------------------------------------------------------------- conversiones
def _row_to_strategy(row: sqlite3.Row) -> Strategy:
    schema_json = row["parameter_schema"]
    return Strategy(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        parameter_schema=json.loads(schema_json) if schema_json is not None else None,
        tags=json.loads(row["tags"]),
        created_at_ms=row["created_at_ms"],
        deleted_at_ms=row["deleted_at_ms"],
    )


def _row_to_version(row: sqlite3.Row) -> StrategyVersion:
    return StrategyVersion(
        strategy_id=row["strategy_id"],
        version=row["version"],
        parameters=json.loads(row["parameters_json"]),
        hash=row["hash"],
        created_at_ms=row["created_at_ms"],
        created_by=row["created_by"],
        description=row["description"],
        tags=json.loads(row["tags"]) if row["tags"] is not None else None,
    )


# --------------------------------------------------------------------------- strategies
def name_exists(conn: sqlite3.Connection, name: str) -> bool:
    return fetch_one(conn, "SELECT 1 FROM strategies WHERE name = ?", (name,)) is not None


def create_strategy_row(  # noqa: PLR0913 - columnas de la tabla
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    name: str,
    description: str | None,
    parameter_schema: dict[str, Any] | None,
    tags: list[str],
    created_at_ms: int,
) -> None:
    """Inserta la fila de ``strategies``.

    Raises:
        StrategyConflictError: SI ``name`` ya existe (índice único, incluye borradas).
    """
    schema_json = json.dumps(parameter_schema) if parameter_schema is not None else None
    try:
        execute(
            conn,
            "INSERT INTO strategies (id, name, description, parameter_schema, tags, created_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_id, name, description, schema_json, json.dumps(tags), created_at_ms),
        )
    except sqlite3.IntegrityError as exc:
        raise StrategyConflictError(name) from exc


def get_strategy_row(conn: sqlite3.Connection, strategy_id: str) -> Strategy | None:
    """Estrategia sin versiones (``versions=[]``) o ``None`` si no existe."""
    row = fetch_one(conn, "SELECT * FROM strategies WHERE id = ?", (strategy_id,))
    return _row_to_strategy(row) if row is not None else None


def update_strategy_metadata(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    description: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Actualiza ``description`` y/o ``tags`` (solo los no ``None``)."""
    if description is not None:
        execute(
            conn, "UPDATE strategies SET description = ? WHERE id = ?", (description, strategy_id)
        )
    if tags is not None:
        execute(
            conn, "UPDATE strategies SET tags = ? WHERE id = ?", (json.dumps(tags), strategy_id)
        )


def soft_delete_strategy(conn: sqlite3.Connection, strategy_id: str, deleted_at_ms: int) -> bool:
    """Marca ``deleted_at_ms`` si aún no estaba borrada. ``True`` si cambió algo."""
    return (
        execute(
            conn,
            "UPDATE strategies SET deleted_at_ms = ? WHERE id = ? AND deleted_at_ms IS NULL",
            (deleted_at_ms, strategy_id),
        )
        > 0
    )


def hard_delete_strategy(conn: sqlite3.Connection, strategy_id: str) -> bool:
    """Elimina la estrategia y, en cascada, todas sus versiones. ``True`` si existía."""
    return execute(conn, "DELETE FROM strategies WHERE id = ?", (strategy_id,)) > 0


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_strategy_rows(
    conn: sqlite3.Connection, filters: StrategyListFilters
) -> tuple[list[StrategySummary], int]:
    """Página de resúmenes (más recientes primero) y ``total_count`` exacto (RF-5)."""
    where = ["1=1"]
    params: list[Any] = []
    if not filters.include_deleted:
        where.append("s.deleted_at_ms IS NULL")
    if filters.tag is not None:
        where.append("EXISTS (SELECT 1 FROM json_each(s.tags) WHERE json_each.value = ?)")
        params.append(filters.tag)
    if filters.name_contains:
        where.append("s.name LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(filters.name_contains)}%")
    clause = " AND ".join(where)

    total_row = fetch_one(conn, f"SELECT COUNT(*) AS n FROM strategies s WHERE {clause}", params)
    total = int(total_row["n"]) if total_row is not None else 0
    rows = fetch_all(
        conn,
        "SELECT s.id, s.name, s.tags, s.deleted_at_ms, v.version, v.hash, v.created_at_ms"
        " FROM strategies s JOIN strategy_versions v ON v.strategy_id = s.id"
        " AND v.version = (SELECT MAX(version) FROM strategy_versions WHERE strategy_id = s.id)"
        f" WHERE {clause} ORDER BY s.created_at_ms DESC, s.id DESC LIMIT ? OFFSET ?",
        [*params, filters.limit, filters.offset],
    )
    items = [
        StrategySummary(
            id=row["id"],
            name=row["name"],
            latest_version=row["version"],
            updated_at_ms=row["created_at_ms"],
            tags=json.loads(row["tags"]),
            hash_latest=row["hash"],
            deleted_at_ms=row["deleted_at_ms"],
        )
        for row in rows
    ]
    return items, total


# ---------------------------------------------------------------------------- versions
def create_version_row(  # noqa: PLR0913 - columnas de la tabla
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    version: int,
    parameters: dict[str, Any],
    hash_: str,
    created_at_ms: int,
    created_by: str | None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> StrategyVersion:
    """Inserta una versión (ADR-S01: solo INSERT). ``(strategy_id, version)`` es único."""
    execute(
        conn,
        f"INSERT INTO strategy_versions ({_VERSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            strategy_id,
            version,
            json.dumps(parameters, ensure_ascii=False),
            hash_,
            created_at_ms,
            created_by,
            description,
            json.dumps(tags) if tags is not None else None,
        ),
    )
    return StrategyVersion(
        strategy_id=strategy_id,
        version=version,
        parameters=parameters,
        hash=hash_,
        created_at_ms=created_at_ms,
        created_by=created_by,
        description=description,
        tags=tags,
    )


def get_version(conn: sqlite3.Connection, strategy_id: str, version: int) -> StrategyVersion | None:
    row = fetch_one(
        conn,
        f"SELECT {_VERSION_COLUMNS} FROM strategy_versions WHERE strategy_id = ? AND version = ?",
        (strategy_id, version),
    )
    return _row_to_version(row) if row is not None else None


def get_latest_version(conn: sqlite3.Connection, strategy_id: str) -> StrategyVersion | None:
    row = fetch_one(
        conn,
        f"SELECT {_VERSION_COLUMNS} FROM strategy_versions WHERE strategy_id = ?"
        " ORDER BY version DESC LIMIT 1",
        (strategy_id,),
    )
    return _row_to_version(row) if row is not None else None


def list_versions(
    conn: sqlite3.Connection, strategy_id: str, limit: int = 50, offset: int = 0
) -> list[StrategyVersion]:
    """Versiones en orden ascendente (v1, v2, ...), paginadas."""
    rows = fetch_all(
        conn,
        f"SELECT {_VERSION_COLUMNS} FROM strategy_versions WHERE strategy_id = ?"
        " ORDER BY version ASC LIMIT ? OFFSET ?",
        (strategy_id, limit, offset),
    )
    return [_row_to_version(row) for row in rows]
