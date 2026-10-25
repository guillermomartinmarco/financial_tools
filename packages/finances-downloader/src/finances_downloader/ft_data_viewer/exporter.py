from pathlib import Path
from typing import Union, IO, Literal, Optional, List
import polars as pl
import structlog
import sys

logger = structlog.get_logger(__name__)


def stream_export(
    lf: pl.LazyFrame,
    format: Literal["parquet", "csv", "jsonl"],
    output: Union[Path, str, IO],
    cols: Optional[List[str]] = None,
) -> int:
    """
    Exporta LazyFrame a Parquet/CSV/JSONL streaming (sin cargar en memoria).
    Retorna número de filas exportadas.
    """
    if cols:
        lf = lf.select(cols)

    # Determinar si output es Path o stream
    is_path = isinstance(output, (Path, str))
    if is_path:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sink = output_path
    else:
        sink = output

    if format == "parquet":
        lf.sink_parquet(sink, compression="zstd")
    elif format == "csv":
        lf.sink_csv(sink)
    elif format == "jsonl":
        lf.sink_ndjson(sink)
    else:
        raise ValueError(f"Formato no soportado: {format}")

    # Para obtener row count, necesitamos contar
    # Nota: sink_* no retorna count, así que hacemos count aparte si es Path
    if is_path:
        count_lf = pl.scan_parquet(output) if format == "parquet" else pl.scan_parquet(output) if format == "csv" else pl.scan_parquet(output)
        # Para CSV/JSONL no hay scan_parquet, usamos count separado
        count_lf = pl.scan_parquet(output) if format == "parquet" else pl.LazyFrame()
        # Fallback: count mediante collect streaming
        count = lf.select(pl.len()).collect()["len"][0]
    else:
        count = lf.select(pl.len()).collect()["len"][0]

    logger.info("export_complete", format=format, rows=count, output=str(output) if is_path else "stream")
    return count


def export_to_stdout(
    lf: pl.LazyFrame,
    format: Literal["parquet", "csv", "jsonl"],
    cols: Optional[List[str]] = None,
) -> int:
    """Exporta a stdout (para piping)."""
    if cols:
        lf = lf.select(cols)

    if format == "parquet":
        lf.sink_parquet(sys.stdout.buffer)
    elif format == "csv":
        lf.sink_csv(sys.stdout)
    elif format == "jsonl":
        lf.sink_ndjson(sys.stdout)
    else:
        raise ValueError(f"Formato no soportado: {format}")

    count = lf.select(pl.len()).collect()["len"][0]
    return count