"""Exportación streaming compartida (``parquet``/``csv``/``jsonl``) con ``sink_*`` de Polars.

Origen: ``ft_data_viewer`` (RF-6, DV-03). Vive en el núcleo (Art. 4) porque la usan
``ft_data_viewer``, ``ft_indicators`` y ``ft_ta_lib`` (plan de ``ft_indicators`` §3.7).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import IO, Literal

import polars as pl
import structlog

logger = structlog.get_logger(__name__)

ExportFormat = Literal["parquet", "csv", "jsonl"]

#: ``Content-Type`` por formato (respuestas ``StreamingResponse`` de la API).
MEDIA_TYPES: dict[str, str] = {
    "parquet": "application/vnd.apache.parquet",
    "csv": "text/csv",
    "jsonl": "application/x-ndjson",
}


class ForbiddenOutputDirError(ValueError):
    """El destino de exportación cae dentro de un directorio de datos protegido."""


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


def validate_output_dir(output_dir: Path | str, forbidden: Sequence[Path]) -> Path:
    """Resuelve ``output_dir`` y verifica que no esté dentro de ningún directorio prohibido.

    Returns:
        Ruta absoluta resuelta del destino.

    Raises:
        ForbiddenOutputDirError: SI el destino está dentro de alguno de ``forbidden``
            (p. ej. ``FT_PARQUET_DIR`` o ``FT_INDICATORS_DIR``).
    """
    target = Path(output_dir).resolve()
    for base in forbidden:
        resolved = base.resolve()
        if target.is_relative_to(resolved):
            msg = f"El directorio de salida no puede estar dentro de {resolved}"
            raise ForbiddenOutputDirError(msg)
    return target
