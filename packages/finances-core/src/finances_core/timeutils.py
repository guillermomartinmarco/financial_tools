"""Utilidades de tiempo compartidas entre herramientas (Art. 4: puente vía finances-core)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d")

# Allowlist explícito por NOMBRE de columnas que contienen un timestamp wall-clock
# en ms epoch UTC (Art. 5). CRÍTICO: no formatear por sufijo "_ms" en bloque;
# p. ej. ``duration_ms`` es una DURACIÓN, no un timestamp, y debe quedar en crudo.
TIMESTAMP_MS_COLUMNS: frozenset[str] = frozenset(
    {
        "open_time_ms",
        "close_time_ms",
        "entry_time_ms",
        "exit_time_ms",
        "event_time_ms",
        "gap_start_ms",
        "gap_end_ms",
        "stable_from_ms",
        "start_ms",
        "end_ms",
        "created_at_ms",
        "updated_at_ms",
    }
)


def format_time_ms(ms: int | None) -> str:
    """Formatea ms epoch UTC a ``"YYYY-MM-DD HH:MM:SS"`` (o ``"-"`` si es None)."""
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def format_table_cell(column: str, value: object) -> str:
    """Celda de tabla CONSOLE: formatea timestamps del allowlist, resto en crudo."""
    if column in TIMESTAMP_MS_COLUMNS and isinstance(value, int):
        return format_time_ms(value)
    return str(value)


def humanize_ms_header(column: str) -> str:
    """Encabezado humano: quita el sufijo ``_ms`` solo en columnas de timestamp."""
    if column in TIMESTAMP_MS_COLUMNS:
        return column.removesuffix("_ms")
    return column


def parse_time_input(s: str) -> int:
    """Parsea una entrada de tiempo flexible a milisegundos UTC (int64, Art. 5).

    Acepta: ``"2024-01-01"``, ``"2024-01-01T00:00:00Z"``, ``"1704067200000"`` y ``"now"``.
    """
    s = s.strip()
    if s.lower() == "now":
        return int(time.time() * 1000)

    if s.isdigit() and len(s) >= 10:  # noqa: PLR2004 - timestamp en ms
        return int(s)

    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return int(dt.timestamp() * 1000)

    msg = f"Formato de tiempo no reconocido: {s}. Use ISO 8601, ms epoch, o 'now'"
    raise ValueError(msg)
