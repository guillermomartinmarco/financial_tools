"""Importación de velas desde Parquet/CSV/JSONL al almacén canónico (RF-4, RF-5, ADR M04).

Fase 1 (``prepare_import``, no escribe nada): resuelve el destino explícito, lee el origen con un
esquema explícito (nunca inferido), valida columnas, valores, timestamp (unidad explícita,
plausibilidad, alineación a la grilla del timeframe y duplicados) y calcula el ``ImportPlan``
junto con el trabajo por partición mensual. Fase 2 vive en ``service.run_import``: combina
(``merge_rows``) y escribe cada partición con ``planner.commit`` (verificación optimista +
``atomic_write``). Las filas existentes nunca cambian de ``open_time_ms``: ``skip`` las conserva
y ``replace`` solo sustituye los valores de una fila con el mismo ``open_time_ms``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import polars as pl
from finances_core.export import ForbiddenOutputDirError, validate_output_dir
from finances_core.marketdata.dataset_layout import (
    dataset_files,
    dataset_label,
    discover_datasets,
    find_dataset_path,
)
from finances_core.marketdata.hive_writer import data_columns, partition_dir
from finances_core.marketdata.timeframes import aligned_expr, timeframe_duration_ms
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.market_data import OHLCV_SCHEMA
from finances_core.schemas.market_data_manager import DatasetSelector, ImportPlan, ImportRequest
from finances_core.schemas.market_data_yfinance import YF_OHLCV_SCHEMA

from finances_downloader.ft_market_data_manager import planner
from finances_downloader.ft_market_data_manager.errors import (
    ConflictError,
    DatasetNotFoundError,
    ImportValidationError,
    InvalidArgumentError,
)

TS_COL = "open_time_ms"
FileFormat = Literal["parquet", "csv", "jsonl"]
OnConflict = Literal["skip", "fail", "replace"]
DataSchema = Mapping[str, pl.DataType | type[pl.DataType]]

#: Esquema canónico (columnas de datos) por exchange cuando el dataset destino aún no existe.
CANONICAL_SCHEMAS = {"binance": OHLCV_SCHEMA, "yfinance": YF_OHLCV_SCHEMA}
#: Columnas que deben ser > 0; el resto de columnas numéricas debe ser >= 0.
PRICE_COLUMNS = frozenset({"open", "high", "low", "close", "adj_close"})

#: 2000-01-01T00:00:00Z y margen superior ``ahora + 1 día`` (RF-5, plausibilidad).
MIN_TS_MS = 946_684_800_000
MAX_AHEAD_MS = 86_400_000
#: Divergencia relativa máxima de ``close`` admitida en ``replace`` sin ``--force`` (1 %).
MAX_CLOSE_DIVERGENCE = 0.01
MAX_EXAMPLES = 3
_INT64_MAX = 2**63 - 1
_UNIT_NAMES = {"s": "segundos", "ms": "milisegundos", "us": "microsegundos", "ns": "nanosegundos"}
#: ``(multiplicar por, dividir entre)`` para llevar cada unidad a milisegundos.
_UNIT_SCALE = {"s": (1000, 1), "ms": (1, 1), "us": (1, 1000), "ns": (1, 1_000_000)}
_EXTENSIONS: dict[str, FileFormat] = {
    ".parquet": "parquet",
    ".csv": "csv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
}
_SEGMENT = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9._=^-]*$")
_INTEGER_TEXT = r"^-?\d+$"
_RAW_COL = "__raw_ts"
_ROW_COL = "__row"
_PROBLEM_COL = "__problem"
_YEAR_COL = "__year"
_MONTH_COL = "__month"


@dataclass(frozen=True)
class ImportTarget:
    """Destino y origen ya resueltos (validación síncrona compartida por CLI y API)."""

    identifier: DatasetIdentifier
    source: Path
    file_format: FileFormat
    root: Path | None  # None => el dataset no existe (solo válido con ``create``)


@dataclass(eq=False)
class PartitionWork:
    """Trabajo de una partición mensual destino calculado en la fase 1."""

    partition: planner.Partition
    existing: pl.DataFrame | None
    incoming: pl.DataFrame
    new_rows: int
    conflicts: int
    values_changed: int  # conflictos cuyos valores difieren de lo existente (relevante en replace)


@dataclass(eq=False)
class ImportPreparation:
    """Resultado de la fase 1: el plan y el trabajo por partición (en orden cronológico)."""

    plan: ImportPlan
    works: list[PartitionWork]
    on_conflict: OnConflict


def _fail(message: str) -> ImportValidationError:
    return ImportValidationError(message)


def _examples(values: list[object]) -> str:
    return ", ".join(str(v) for v in values[:MAX_EXAMPLES])


def _iso(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return "fuera de rango"


# --- Destino y origen (validación síncrona) ------------------------------------------------


def _identifier(request: ImportRequest) -> DatasetIdentifier:
    for field in ("exchange", "market_type", "symbol", "timeframe"):
        value = getattr(request, field)
        if field != "market_type" and not _SEGMENT.fullmatch(value):
            msg = f"{field} inválido: {value!r} (solo letras, dígitos y . _ = ^ -)"
            raise InvalidArgumentError(msg)
    try:
        timeframe_duration_ms(request.timeframe)
        return DatasetIdentifier.model_validate(
            {
                "exchange": request.exchange,
                "market_type": request.market_type,
                "symbol": request.symbol,
                "timeframe": request.timeframe,
            }
        )
    except ValueError as exc:  # timeframe desconocido o market_type inválido
        raise InvalidArgumentError(str(exc)) from exc


def _file_format(request: ImportRequest) -> FileFormat:
    if request.format is not None:
        return request.format
    found = _EXTENSIONS.get(request.source.suffix.lower())
    if found is None:
        msg = (
            f"no se pudo deducir el formato de {request.source.name!r}: "
            "indique --format parquet|csv|jsonl"
        )
        raise InvalidArgumentError(msg)
    return found


def _check_source(request: ImportRequest, parquet_dir: Path) -> Path:
    try:
        source = validate_output_dir(request.source, [parquet_dir])
    except ForbiddenOutputDirError as exc:
        msg = "el archivo de origen no puede estar dentro de FT_PARQUET_DIR (el almacén)"
        raise InvalidArgumentError(msg) from exc
    if not source.is_file():
        msg = f"archivo de origen no encontrado: {request.source}"
        raise DatasetNotFoundError(msg)
    return source


def _reject_case_variant(identifier: DatasetIdentifier, parquet_dir: Path) -> None:
    """Un destino que solo difiere en mayúsculas de un dataset existente no se acepta.

    En sistemas de archivos insensibles a mayúsculas (Windows) resolvería al dataset existente y
    la importación quedaría en un ``conflict`` silencioso; en los sensibles (Linux) con ``create``
    crearía un dataset fantasma paralelo.
    """
    wanted = dataset_label(identifier)
    for existing, _ in discover_datasets(parquet_dir):
        label = dataset_label(existing)
        if label != wanted and label.lower() == wanted.lower():
            msg = (
                f"el dataset destino {wanted} difiere solo en capitalización del existente "
                f"{label}; use exactamente {label}"
            )
            raise InvalidArgumentError(msg)


def resolve_target(request: ImportRequest, parquet_dir: Path) -> ImportTarget:
    """Valida destino, origen y formato sin leer el contenido (la API lo hace antes de la tarea).

    Raises:
        InvalidArgumentError: SI el destino tiene valores inválidos, el origen está dentro de
            ``FT_PARQUET_DIR`` o no se puede deducir el formato.
        DatasetNotFoundError: SI el archivo de origen no existe o el dataset destino no existe
            y no se indicó ``create``.
    """
    identifier = _identifier(request)
    _reject_case_variant(identifier, parquet_dir)
    source = _check_source(request, parquet_dir)
    file_format = _file_format(request)
    root = find_dataset_path(parquet_dir, identifier)
    if root is None and not request.create:
        msg = (
            f"el dataset destino {dataset_label(identifier)} no existe; "
            "use --create (create=true) para crearlo"
        )
        raise DatasetNotFoundError(msg)
    return ImportTarget(identifier, source, file_format, root)


def _destination_schema(target: ImportTarget) -> DataSchema:
    """Esquema de datos del destino: el de sus archivos si existe; si no, el canónico."""
    if target.root is not None:
        first = dataset_files(target.root)[0]
        try:
            return dict(pl.read_parquet_schema(first))
        except pl.exceptions.PolarsError as exc:
            msg = f"archivo existente ilegible ({first.name}): {exc}"
            raise _fail(msg) from exc
    canonical = CANONICAL_SCHEMAS.get(target.identifier.exchange)
    if canonical is None:
        msg = (
            f"no hay esquema canónico para el exchange {target.identifier.exchange!r}; "
            f"solo se puede crear en: {', '.join(sorted(CANONICAL_SCHEMAS))}"
        )
        raise _fail(msg)
    return {name: canonical[name] for name in data_columns(canonical)}


# --- Lectura del origen --------------------------------------------------------------------


def _scan(path: Path, file_format: FileFormat) -> pl.LazyFrame:
    """Lectura perezosa. CSV/JSONL se leen con esquema explícito: todo ``String``; los tipos
    los decide este módulo (nunca se infieren)."""
    if file_format == "parquet":
        return pl.scan_parquet(path)
    if file_format == "csv":
        names = pl.read_csv(path, n_rows=0).columns
        return pl.scan_csv(path, schema=dict.fromkeys(names, pl.String))
    names = pl.scan_ndjson(path, infer_schema_length=None).collect_schema().names()
    return pl.scan_ndjson(path, schema=dict.fromkeys(names, pl.String))


def _read_source(target: ImportTarget, expected: list[str], ts_column: str) -> pl.DataFrame:
    """Lee el origen, exige exactamente las columnas esperadas y renombra la de tiempo."""
    try:
        lazy = _scan(target.source, target.file_format)
        names = lazy.collect_schema().names()
        _check_columns(names, [ts_column if c == TS_COL else c for c in expected])
        frame = lazy.collect()
    except pl.exceptions.PolarsError as exc:
        msg = f"archivo de origen ilegible ({target.file_format}): {exc}"
        raise _fail(msg) from exc
    return frame.rename({ts_column: TS_COL}) if ts_column != TS_COL else frame


def _check_columns(found: list[str], expected: list[str]) -> None:
    missing = [c for c in expected if c not in found]
    extra = [c for c in found if c not in expected]
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"faltan columnas: {', '.join(missing)}")
        if extra:
            parts.append(f"sobran columnas: {', '.join(extra)}")
        msg = f"{'; '.join(parts)} (esperadas: {', '.join(expected)})"
        raise _fail(msg)


# --- Tipos y valores -----------------------------------------------------------------------


def _check_types(raw: pl.DataFrame, schema: DataSchema, *, textual: bool) -> None:
    """Rechaza orígenes cuyos tipos no admiten una conversión sin pérdida de significado."""
    for name, dtype in schema.items():
        source_dtype = raw.schema[name]
        if dtype.is_float():
            allowed = textual or source_dtype.is_numeric()
            kind = "numérica"
        elif dtype.is_integer():
            allowed = textual or source_dtype.is_integer()
            kind = "entera"
        else:
            msg = f"la columna {name!r} del destino tiene un tipo no soportado ({dtype})"
            raise _fail(msg)
        if not allowed:
            msg = f"la columna {name!r} debe ser {kind}, es {source_dtype}"
            raise _fail(msg)
    if textual:
        _check_integer_text(raw[TS_COL])


def _check_integer_text(column: pl.Series) -> None:
    bad = column.filter(column.is_not_null() & ~column.str.contains(_INTEGER_TEXT))
    if bad.len():
        msg = (
            f"la columna de tiempo debe ser un entero (int64) en la unidad indicada; "
            f"{bad.len()} valores no lo son (ejemplo: {bad[0]!r}). "
            "Los timestamps ISO-8601 no se aceptan (Art. 5.4)"
        )
        raise _fail(msg)


def _typed(raw: pl.DataFrame, schema: DataSchema) -> pl.DataFrame:
    exprs: list[pl.Expr] = []
    for name, dtype in schema.items():
        column = pl.col(name)
        if dtype.is_float():
            expr = column.cast(pl.Float64, strict=False).cast(dtype, strict=False)
        else:
            expr = column.cast(pl.Int64, strict=False).cast(dtype, strict=False)
        exprs.append(expr.alias(name))
    return raw.select(exprs)


def _problem_expr(schema: DataSchema) -> pl.Expr:
    """Nombre de la primera columna con valor inválido (nulo, no finito o fuera de rango)."""
    parts: list[pl.Expr] = []
    for name, dtype in schema.items():
        column = pl.col(name)
        if name == TS_COL:
            valid = column.is_not_null()
        elif dtype.is_float():
            valid = column.is_finite() & (column > 0 if name in PRICE_COLUMNS else column >= 0)
        else:
            valid = column >= 0
        parts.append(pl.when(~valid.fill_null(value=False)).then(pl.lit(name)))
    return pl.coalesce(parts)


def _drop_invalid(
    df: pl.DataFrame, schema: DataSchema, *, skip_invalid: bool
) -> tuple[pl.DataFrame, int, list[str]]:
    """Separa las filas inválidas. Devuelve (válidas, nº rechazadas, ejemplos)."""
    flagged = df.with_row_index(_ROW_COL, offset=1).with_columns(
        _problem_expr(schema).alias(_PROBLEM_COL)
    )
    bad = flagged.filter(pl.col(_PROBLEM_COL).is_not_null())
    if bad.height == 0:
        return df, 0, []
    examples = [
        f"fila {row[_ROW_COL]} (columna {row[_PROBLEM_COL]}, {TS_COL}={row[TS_COL]})"
        for row in bad.head(MAX_EXAMPLES).iter_rows(named=True)
    ]
    if not skip_invalid:
        msg = (
            f"{bad.height} filas inválidas (nulos, no finitos, precio <= 0 o volumen < 0); "
            f"ejemplos: {'; '.join(examples)}. Use --skip-invalid para importar solo las válidas"
        )
        raise _fail(msg)
    valid = flagged.filter(pl.col(_PROBLEM_COL).is_null()).drop(_ROW_COL, _PROBLEM_COL)
    return valid, bad.height, examples


# --- Timestamp -----------------------------------------------------------------------------


def _to_milliseconds(df: pl.DataFrame, unit: str) -> pl.DataFrame:
    """Convierte la columna de tiempo de ``unit`` a ms (único punto de conversión)."""
    multiply, divide = _UNIT_SCALE[unit]
    raw = pl.col(TS_COL)
    if divide > 1 and df.filter(raw % divide != 0).height:
        msg = (
            f"hay timestamps en {_UNIT_NAMES[unit]} con precisión sub-milisegundo "
            f"(no divisibles entre {divide}); no se redondean"
        )
        raise _fail(msg)
    limit = _INT64_MAX // multiply
    converted = raw.clip(-limit, limit) * multiply // divide
    return df.with_columns(raw.alias(_RAW_COL)).with_columns(converted.alias(TS_COL))


def _unit_hint(raw_value: int, unit: str, lower: int, upper: int) -> str:
    for candidate, (multiply, divide) in _UNIT_SCALE.items():
        if candidate != unit and lower <= raw_value * multiply // divide <= upper:
            return (
                f"los valores parecen estar en {_UNIT_NAMES[candidate]}; use --ts-unit {candidate}"
            )
    return "revise la columna de tiempo y --ts-unit"


def _check_plausible(df: pl.DataFrame, unit: str, now_ms: int) -> None:
    upper = now_ms + MAX_AHEAD_MS
    outside = df.filter(~pl.col(TS_COL).is_between(MIN_TS_MS, upper))
    if outside.height == 0:
        return
    first = int(outside[_RAW_COL][0])
    msg = (
        f"{outside.height} timestamps fuera del rango plausible "
        f"(2000-01-01 UTC .. ahora + 1 día) con --ts-unit {unit}; ejemplo: {first}. "
        f"{_unit_hint(first, unit, MIN_TS_MS, upper).capitalize()}"
    )
    raise _fail(msg)


def _check_alignment(df: pl.DataFrame, timeframe: str) -> None:
    off = df.filter(~aligned_expr(pl.col(TS_COL), timeframe))
    if off.height == 0:
        return
    example = int(off[TS_COL][0])
    msg = (
        f"{off.height} filas desalineadas con la grilla del timeframe {timeframe} "
        f"(ejemplo: {example} = {_iso(example)}); revise zona horaria o timeframe. "
        "No se redondean ni se desplazan"
    )
    raise _fail(msg)


def _check_unique(df: pl.DataFrame) -> None:
    repeated = df.filter(pl.col(TS_COL).is_duplicated())[TS_COL].unique().sort()
    if repeated.len():
        msg = (
            f"{TS_COL} duplicados en el origen ({repeated.len()} valores repetidos); "
            f"ejemplos: {_examples(repeated.to_list())}. No se elige una fila en silencio"
        )
        raise _fail(msg)


# --- Combinación con el almacén ------------------------------------------------------------


def _read_existing(partition: planner.Partition, schema: DataSchema) -> pl.DataFrame:
    where = f"{partition.label} {partition.period}"
    try:
        frame = pl.concat([pl.read_parquet(f) for f in partition.files], how="vertical")
    except (pl.exceptions.PolarsError, ValueError) as exc:
        msg = f"partición existente ilegible o con esquemas dispares ({where}): {exc}"
        raise _fail(msg) from exc
    if list(frame.schema.items()) != list(schema.items()):
        msg = f"la partición existente {where} no tiene el esquema del dataset ({frame.schema})"
        raise _fail(msg)
    return frame


def _divergence(conflicting: pl.DataFrame, existing: pl.DataFrame) -> float | None:
    """Divergencia relativa máxima de ``close`` = max(|nuevo - viejo| / viejo)."""
    if conflicting.height == 0 or "close" not in existing.columns:
        return None
    new = conflicting.select(TS_COL, pl.col("close").cast(pl.Float64).alias("new"))
    old = existing.select(TS_COL, pl.col("close").cast(pl.Float64).alias("old"))
    joined = new.join(old, on=TS_COL)
    ratio = pl.when(pl.col("old") > 0).then((pl.col("new") - pl.col("old")).abs() / pl.col("old"))
    value = joined.select(ratio.otherwise(1e18).max()).item()
    return None if value is None else float(value)


def _work_for(
    partition: planner.Partition, incoming: pl.DataFrame, schema: DataSchema
) -> tuple[PartitionWork, float | None]:
    if not partition.files:
        return PartitionWork(partition, None, incoming, incoming.height, 0, 0), None
    existing = _read_existing(partition, schema)
    is_conflict = pl.col(TS_COL).is_in(existing[TS_COL].implode())
    conflicting = incoming.filter(is_conflict)
    identical = conflicting.join(existing.unique(), on=list(schema), how="semi").height
    work = PartitionWork(
        partition,
        existing,
        incoming,
        incoming.height - conflicting.height,
        conflicting.height,
        conflicting.height - identical,
    )
    return work, _divergence(conflicting, existing)


def _build_works(
    target: ImportTarget, df: pl.DataFrame, schema: DataSchema, parquet_dir: Path
) -> tuple[list[PartitionWork], float | None]:
    ident = target.identifier
    selector = DatasetSelector(
        exchange=[ident.exchange],
        market_type=[ident.market_type],
        symbol=[ident.symbol],
        timeframe=[ident.timeframe],
    )
    existing = {(p.year, p.month): p for p in planner.plan(selector, parquet_dir)}
    stamped = df.with_columns(
        pl.from_epoch(TS_COL, time_unit="ms").dt.year().alias(_YEAR_COL),
        pl.from_epoch(TS_COL, time_unit="ms").dt.month().alias(_MONTH_COL),
    )
    groups = stamped.partition_by([_YEAR_COL, _MONTH_COL], as_dict=True, maintain_order=True)
    works: list[PartitionWork] = []
    divergences: list[float] = []
    for raw_key in sorted(groups, key=lambda k: (int(str(k[0])), int(str(k[1])))):
        year, month = int(str(raw_key[0])), int(str(raw_key[1]))
        incoming = groups[raw_key].drop(_YEAR_COL, _MONTH_COL).sort(TS_COL)
        found = existing.get((year, month))
        partition = found or planner.Partition(
            dataset=ident,
            year=year,
            month=month,
            directory=partition_dir(
                parquet_dir,
                ident.exchange,
                (ident.market_type, ident.symbol, ident.timeframe, year, month),
            ),
            files=(),
            snapshot=(),
        )
        work, divergence = _work_for(partition, incoming, schema)
        works.append(work)
        if divergence is not None:
            divergences.append(divergence)
    return works, max(divergences, default=None)


def merge_rows(work: PartitionWork, on_conflict: OnConflict) -> pl.DataFrame:
    """Contenido final de la partición, ordenado por ``open_time_ms`` (orden estable).

    ``skip``/``fail``: las filas existentes se conservan intactas y se añaden las nuevas.
    ``replace``: se sustituyen solo los valores de las filas con el mismo ``open_time_ms``.
    """
    if work.existing is None:
        return work.incoming.sort(TS_COL)
    incoming_ts = work.incoming[TS_COL].implode()
    existing_ts = work.existing[TS_COL].implode()
    if on_conflict == "replace":
        kept = work.existing.filter(~pl.col(TS_COL).is_in(incoming_ts))
        merged = pl.concat([kept, work.incoming], how="vertical")
    else:
        added = work.incoming.filter(~pl.col(TS_COL).is_in(existing_ts))
        merged = pl.concat([work.existing, added], how="vertical")
    return merged.sort(TS_COL, maintain_order=True)


def modifies(work: PartitionWork, on_conflict: OnConflict) -> bool:
    """SI la partición cambiaría: filas nuevas o (``replace``) valores distintos."""
    if work.existing is None or work.new_rows > 0:
        return True
    return on_conflict == "replace" and work.values_changed > 0


# --- Fase 1 --------------------------------------------------------------------------------


def _check_conflicts(request: ImportRequest, conflicts: int, divergence: float | None) -> None:
    if request.on_conflict == "fail" and conflicts:
        msg = f"{conflicts} conflictos con filas existentes y --on-conflict fail"
        raise ConflictError(msg)
    too_far = divergence is not None and divergence > MAX_CLOSE_DIVERGENCE
    if request.on_conflict == "replace" and too_far and not request.force:
        msg = (
            f"divergencia máxima de close {divergence:.2%} > {MAX_CLOSE_DIVERGENCE:.0%} "
            "entre el archivo y lo existente (indicio de timestamp desplazado); "
            "use --force para reemplazar igualmente"
        )
        raise ConflictError(msg)


def _load_valid_rows(
    request: ImportRequest, target: ImportTarget, schema: DataSchema, now_ms: int
) -> tuple[pl.DataFrame, int, int, list[str]]:
    """Lee, tipa y valida el origen. Devuelve (filas válidas en ms, total, rechazadas, ejemplos)."""
    expected = list(schema)
    raw = _read_source(target, expected, request.ts_column)
    textual = target.file_format != "parquet"
    _check_types(raw, schema, textual=textual)
    typed = _typed(raw, schema)
    valid, rejected, examples = _drop_invalid(typed, schema, skip_invalid=request.skip_invalid)
    if valid.height == 0:
        msg = "no hay filas válidas para importar"
        raise _fail(msg)
    converted = _to_milliseconds(valid, request.ts_unit)
    _check_plausible(converted, request.ts_unit, now_ms)
    _check_alignment(converted, request.timeframe)
    _check_unique(converted)
    return converted.drop(_RAW_COL), typed.height, rejected, examples


def _range_ms(rows: pl.DataFrame) -> tuple[int, int]:
    low, high = rows.select(pl.col(TS_COL).min().alias("lo"), pl.col(TS_COL).max().alias("hi")).row(
        0
    )
    return int(low), int(high)


def prepare_import(
    request: ImportRequest, parquet_dir: Path, now_ms: int | None = None
) -> ImportPreparation:
    """Fase 1: valida todo y calcula el plan sin escribir nada.

    Raises:
        InvalidArgumentError: SI el destino/origen/formato es inválido.
        DatasetNotFoundError: SI el origen o el dataset destino (sin ``create``) no existe.
        ImportValidationError: SI las columnas, los tipos, los valores (sin ``skip_invalid``),
            el timestamp, la alineación o los duplicados del archivo son inválidos.
        ConflictError: SI ``on_conflict=fail`` con conflictos, o ``replace`` con divergencia
            de ``close`` > 1 % sin ``force``.
    """
    target = resolve_target(request, parquet_dir)
    schema = _destination_schema(target)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    rows, total, rejected, examples = _load_valid_rows(request, target, schema, now)
    works, divergence = _build_works(target, rows, schema, parquet_dir)
    conflicts = sum(w.conflicts for w in works)
    _check_conflicts(request, conflicts, divergence)
    plan = ImportPlan(
        destination=dataset_label(target.identifier),
        rows_in_file=total,
        rows_new=sum(w.new_rows for w in works),
        rows_conflict=conflicts,
        rows_rejected=rejected,
        range_ms=_range_ms(rows),
        partitions_new=[w.partition.period for w in works if w.existing is None],
        partitions_modified=[
            w.partition.period
            for w in works
            if w.existing is not None and modifies(w, request.on_conflict)
        ],
        max_close_divergence=divergence,
        rejected_examples=examples,
    )
    return ImportPreparation(plan=plan, works=works, on_conflict=request.on_conflict)


__all__ = [
    "ImportPreparation",
    "ImportTarget",
    "PartitionWork",
    "merge_rows",
    "modifies",
    "prepare_import",
    "resolve_target",
]
