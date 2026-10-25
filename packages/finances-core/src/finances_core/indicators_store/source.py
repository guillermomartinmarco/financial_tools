"""Fuente Hive de solo lectura de los indicadores (plan de ``ft_indicators`` §3.2).

Layout (escrito por los descargadores con :mod:`finances_core.marketdata.hive_writer`)::

    {FT_PARQUET_DIR}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/year=YYYY/month=MM/data.parquet

Este módulo **nunca** escribe bajo ``FT_PARQUET_DIR`` (RF-1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import structlog

from finances_core.config import FinancesSettings
from finances_core.marketdata.inventory import partition_base_dir
from finances_core.schemas.data_viewer import DatasetIdentifier

logger = structlog.get_logger(__name__)

TIME_COLUMN = "open_time_ms"
SOURCE_FILE_NAME = "data.parquet"
#: Columnas canónicas enteras (Art. 5); el resto de columnas numéricas son ``Float32``.
INT64_COLUMNS = frozenset({TIME_COLUMN, "trades_count"})


class SourceNotFoundError(LookupError):
    """El dataset no existe (o desapareció) bajo ``FT_PARQUET_DIR``."""

    code = "dataset_not_found"


class InvalidSourceError(ValueError):
    """Fuente no canónica (Art. 13): columnas, dtypes o ``open_time_ms`` inválidos."""

    code = "invalid_source"


@dataclass(frozen=True)
class SourceFrame:
    """Serie fuente completa cargada en memoria (``open_time_ms`` + columnas pedidas)."""

    dataset: DatasetIdentifier
    frame: pl.DataFrame
    files: tuple[Path, ...]


@dataclass(frozen=True)
class ValidatedSource:
    """Fuente validada: no finitos convertidos a nulo y conteo de nulos por columna."""

    frame: pl.DataFrame
    null_counts: dict[str, int]


def dataset_dir(settings: FinancesSettings, ds: DatasetIdentifier) -> Path:
    """Directorio Hive ``{exchange}/market_type=…/symbol=…/timeframe=…`` de ``ds``."""
    return partition_base_dir(
        settings.PARQUET_DIR, ds.exchange, ds.symbol, ds.timeframe, ds.market_type
    )


def source_files(settings: FinancesSettings, ds: DatasetIdentifier) -> list[Path]:
    """Archivos mensuales de ``ds`` en orden cronológico (claves ``year``/``month`` con ceros)."""
    base = dataset_dir(settings, ds)
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob(f"year=*/month=*/{SOURCE_FILE_NAME}") if p.is_file())


def dataset_exists(settings: FinancesSettings, ds: DatasetIdentifier) -> bool:
    """``True`` si ``ds`` tiene al menos un archivo mensual (solo ``stat``, sin leer datos)."""
    return bool(source_files(settings, ds))


def _require_files(settings: FinancesSettings, ds: DatasetIdentifier) -> list[Path]:
    files = source_files(settings, ds)
    if not files:
        msg = f"Dataset no encontrado: {ds.path_segment()}"
        raise SourceNotFoundError(msg)
    return files


def available_columns(settings: FinancesSettings, ds: DatasetIdentifier) -> set[str]:
    """Columnas de ``ds`` leyendo solo el esquema (*footer*) del último archivo mensual.

    Raises:
        SourceNotFoundError: SI el dataset no existe.
        InvalidSourceError: SI el *footer* es ilegible.
    """
    files = _require_files(settings, ds)
    try:
        return set(pl.read_parquet_schema(files[-1]))
    except (pl.exceptions.PolarsError, OSError) as exc:
        msg = f"Esquema ilegible en {files[-1]}: {exc}"
        raise InvalidSourceError(msg) from exc


def load_source(
    settings: FinancesSettings, ds: DatasetIdentifier, columns: Sequence[str]
) -> SourceFrame:
    """Carga la serie **completa** de ``ds`` (solo ``open_time_ms`` + ``columns``).

    Un único ``collect()`` sobre ``scan_parquet`` de los archivos en orden cronológico;
    el orden y la unicidad de ``open_time_ms`` los verifica :func:`validate_source`.

    Raises:
        SourceNotFoundError: SI el dataset no existe o desaparece durante la lectura.
        InvalidSourceError: SI faltan columnas o los archivos son ilegibles/incoherentes.
    """
    files = _require_files(settings, ds)
    wanted = [TIME_COLUMN, *(c for c in dict.fromkeys(columns) if c != TIME_COLUMN)]
    try:
        lf = pl.scan_parquet(files, hive_partitioning=False)
        present = set(lf.collect_schema().names())
        missing = [c for c in wanted if c not in present]
        if missing:
            msg = f"Columnas ausentes en {ds.path_segment()}: {missing}"
            raise InvalidSourceError(msg)
        frame = lf.select(wanted).collect()
    except FileNotFoundError as exc:
        msg = f"Dataset desaparecido durante la lectura: {ds.path_segment()}"
        raise SourceNotFoundError(msg) from exc
    except (pl.exceptions.PolarsError, OSError) as exc:
        msg = f"Fuente ilegible {ds.path_segment()}: {exc}"
        raise InvalidSourceError(msg) from exc
    logger.debug("source_loaded", dataset=ds.path_segment(), rows=frame.height, files=len(files))
    return SourceFrame(dataset=ds, frame=frame, files=tuple(files))


def _expected_dtype(column: str) -> pl.DataType:
    return pl.Int64() if column in INT64_COLUMNS else pl.Float32()


def validate_source(df: pl.DataFrame, required: Sequence[str]) -> ValidatedSource:
    """Valida la fuente (Art. 13) y normaliza no finitos.

    - ``open_time_ms`` ``Int64``, sin nulos, estrictamente creciente y único.
    - Columnas requeridas presentes con su dtype canónico (``Float32``; ``Int64`` en
      ``trades_count``).
    - ``NaN``/``±inf`` → nulo; devuelve el conteo de nulos por columna requerida.

    Raises:
        InvalidSourceError: ante cualquier incumplimiento.
    """
    columns = [c for c in dict.fromkeys(required) if c != TIME_COLUMN]
    missing = [c for c in [TIME_COLUMN, *columns] if c not in df.columns]
    if missing:
        msg = f"Columnas requeridas ausentes: {missing}"
        raise InvalidSourceError(msg)
    for col in [TIME_COLUMN, *columns]:
        expected = _expected_dtype(col)
        if df.schema[col] != expected:
            msg = f"Columna {col!r} con dtype {df.schema[col]} (se espera {expected})"
            raise InvalidSourceError(msg)

    times = df[TIME_COLUMN]
    if times.null_count() > 0:
        msg = "open_time_ms contiene nulos"
        raise InvalidSourceError(msg)
    if df.height > 1 and not bool((times.diff().drop_nulls() > 0).all()):
        msg = "open_time_ms no es estrictamente creciente y único"
        raise InvalidSourceError(msg)

    float_cols = [c for c in columns if df.schema[c] == pl.Float32]
    clean = df.with_columns(
        [
            pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c)
            for c in float_cols
        ]
    )
    null_counts = {c: int(clean[c].null_count()) for c in columns}
    return ValidatedSource(frame=clean, null_counts=null_counts)
