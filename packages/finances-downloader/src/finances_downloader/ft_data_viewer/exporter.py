"""Exportación streaming de subconjuntos (RF-6, DV-03): ``sink_parquet/csv/ndjson``."""

from __future__ import annotations

from pathlib import Path
from typing import IO, Literal

import polars as pl
import structlog

logger = structlog.get_logger(__name__, component="ft_data_viewer")

ExportFormat = Literal["parquet", "csv", "jsonl"]

#: ``Content-Type`` por formato (API, T6.3).
MEDIA_TYPES: dict[str, str] = {
    "parquet": "application/vnd.apache.parquet",
    "csv": "text/csv",
    "jsonl": "application/x-ndjson",
}


def stream_export(lf: pl.LazyFrame, fmt: ExportFormat, output: Path | IO[bytes]) -> int:
    """Exporta ``lf`` en streaming a un archivo o a un stream binario.

    Returns:
        Número de filas exportadas (conteo lazy, resuelto desde metadatos cuando es posible).

    Raises:
        ValueError: SI ``fmt`` no es ``parquet``, ``csv`` o ``jsonl``.
        OSError: SI el destino no se puede crear o escribir.
    """
    if fmt not in MEDIA_TYPES:
        msg = f"Formato no soportado: {fmt!r} (parquet|csv|jsonl)"
        raise ValueError(msg)
    if isinstance(output, Path):
        output.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "parquet":
        lf.sink_parquet(output, compression="zstd")
    elif fmt == "csv":
        lf.sink_csv(output)
    else:
        lf.sink_ndjson(output)

    rows = int(lf.select(pl.len()).collect().item())
    logger.debug(
        "export_written",
        format=fmt,
        rows=rows,
        output=str(output) if isinstance(output, Path) else "stream",
    )
    return rows
