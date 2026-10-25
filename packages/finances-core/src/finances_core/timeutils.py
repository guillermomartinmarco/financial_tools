"""Utilidades de tiempo compartidas entre herramientas (Art. 4: puente vía finances-core)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d")


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
