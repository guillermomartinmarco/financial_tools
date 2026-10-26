"""Hash de contenido canónico ``ftds-v1`` (plan §3.2, ADR-D02).

El hash se define sobre los **valores almacenados**, no sobre los bytes del Parquet::

    col_digest(name, dtype, n, values, nulls) =
        SHA256( b"ftds-v1\\0col\\0" + name + b"\\0" + dtype + b"\\0" + u64le(n)
                + SHA256(values_stream) + SHA256(null_positions_stream) )
    values_stream:   int64   -> 8 bytes LE por fila (nulo -> 8 bytes 0)
                     float32 -> 4 bytes LE del patrón IEEE (NaN -> 0x7FC00000; nulo -> 0)
    null_positions:  u64le(índice de fila) por cada nulo
    content_hash   = "sha256:" + SHA256( b"ftds-v1\\0item\\0"
                                         + Σ_strict (name + b"\\0" + col_digest) )
    month_hashes[m]= content_hash restringido a las filas del mes UTC ``m`` (índices locales)

No se ejecuta aritmética de coma flotante: solo se reinterpretan bits, por lo que el
resultado es idéntico en cualquier SO/CPU. La única variación conocida de bits entre CPUs
sin aritmética (la carga útil de NaN) se canonicaliza; ``-0.0`` se conserva (es dato).
El cómputo es en *streaming*: :class:`ItemHasher` recibe trozos (p. ej. un mes) y
mantiene un *hasher* por columna y por mes, sin cargar el rango entero (Art. 19).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from finances_core.datasets.errors import DatasetDataError
from finances_core.indicators_store.source import INT64_COLUMNS, TIME_COLUMN

ALGORITHM = b"ftds-v1"
#: Patrón canónico de NaN (``0x7FC00000``, NaN silencioso positivo).
CANONICAL_NAN_BITS = 0x7FC00000
_U32 = np.dtype("<u4")
_F32 = np.dtype("<f4")
_I64 = np.dtype("<i8")
_U64 = np.dtype("<u8")

#: Columnas estrictas por exchange, en orden fijo (RF-3).
STRICT_COLUMNS: dict[str, tuple[str, ...]] = {
    "binance": (
        "open_time_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades_count",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
    ),
    "yfinance": ("open_time_ms", "open", "high", "low", "close", "volume"),
}
#: Columnas informativas (su cambio es aviso, nunca fallo; ADR-D05).
INFORMATIONAL_COLUMNS: dict[str, tuple[str, ...]] = {"binance": (), "yfinance": ("adj_close",)}
#: Columna de las sondas (RF-6).
PROBE_COLUMN = "close"


def dtype_tag(column: str) -> str:
    """Dtype canónico de ``column`` en el hash (``int64`` o ``float32``)."""
    return "int64" if column in INT64_COLUMNS else "float32"


def _expected_dtype(column: str) -> pl.DataType:
    return pl.Int64() if column in INT64_COLUMNS else pl.Float32()


def _prefixed(digest: bytes) -> str:
    return "sha256:" + digest.hex()


def float32_bits(series: pl.Series) -> np.ndarray[Any, np.dtype[np.uint32]]:
    """Patrones de bits canónicos de una serie ``Float32`` (NaN canónico; nulo -> 0)."""
    values = np.ascontiguousarray(series.to_numpy(), dtype=_F32)
    bits = np.where(np.isnan(values), np.uint32(CANONICAL_NAN_BITS), values.view(_U32))
    if series.null_count():
        bits[series.is_null().to_numpy()] = 0
    return bits.astype(_U32, copy=False)


def _value_bytes(series: pl.Series, tag: str) -> bytes:
    if tag == "int64":
        filled = series.fill_null(0) if series.null_count() else series
        return np.ascontiguousarray(filled.to_numpy(), dtype=_I64).tobytes()
    return float32_bits(series).tobytes()


def check_chunk(frame: pl.DataFrame, columns: Sequence[str], previous_last: int | None) -> None:
    """Valida un trozo (Art. 13, RF-3): columnas presentes con dtype canónico y
    ``open_time_ms`` sin nulos, estrictamente creciente y posterior a ``previous_last``.

    Raises:
        DatasetDataError: ante cualquier incumplimiento (dato no canónico o duplicado).
    """
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        msg = f"Columnas ausentes en los datos: {missing}"
        raise DatasetDataError(msg)
    for column in columns:
        expected = _expected_dtype(column)
        if frame.schema[column] != expected:
            msg = f"Columna {column!r} con dtype {frame.schema[column]} (se espera {expected})"
            raise DatasetDataError(msg)
    if frame.height == 0:
        return
    times = frame[TIME_COLUMN]
    if times.null_count():
        msg = f"{TIME_COLUMN} contiene nulos"
        raise DatasetDataError(msg)
    ordered = frame.height <= 1 or bool((times.diff().drop_nulls() > 0).all())
    if not ordered or (previous_last is not None and int(times[0]) <= previous_last):
        msg = f"{TIME_COLUMN} no es estrictamente creciente y único (duplicado o desorden)"
        raise DatasetDataError(msg)


class _ColumnDigest:
    """Acumulador *streaming* de ``col_digest`` (índices de nulos relativos a su inicio)."""

    __slots__ = ("_nulls", "_values", "name", "rows", "tag")

    def __init__(self, name: str) -> None:
        self.name = name
        self.tag = dtype_tag(name)
        self.rows = 0
        self._values = hashlib.sha256()
        self._nulls = hashlib.sha256()

    def update(self, series: pl.Series) -> None:
        self._values.update(_value_bytes(series, self.tag))
        if series.null_count():
            positions = series.is_null().arg_true().to_numpy().astype(_U64) + np.uint64(self.rows)
            self._nulls.update(positions.astype(_U64, copy=False).tobytes())
        self.rows += series.len()

    def digest(self) -> bytes:
        header = (
            ALGORITHM + b"\0col\0" + self.name.encode("utf-8") + b"\0" + self.tag.encode() + b"\0"
        )
        return hashlib.sha256(
            header + self.rows.to_bytes(8, "little") + self._values.digest() + self._nulls.digest()
        ).digest()


def _content_hash(digests: Iterable[_ColumnDigest]) -> str:
    body = b"".join(d.name.encode("utf-8") + b"\0" + d.digest() for d in digests)
    return _prefixed(hashlib.sha256(ALGORITHM + b"\0item\0" + body).digest())


@dataclass(frozen=True)
class ProbeValue:
    """Valor de la columna de sondas en una fila (``close_bits`` ``None`` = nulo)."""

    row: int
    open_time_ms: int
    close_bits: int | None


@dataclass(frozen=True)
class HashResult:
    """Resultado ``ftds-v1`` de un ítem."""

    row_count: int
    first_open_time_ms: int | None
    last_open_time_ms: int | None
    content_hash: str
    column_hashes: dict[str, str]
    month_hashes: dict[str, str]
    informational: dict[str, str]
    probes: tuple[ProbeValue, ...] = field(default=())


class ItemHasher:
    """Hash ``ftds-v1`` en *streaming* (trozos en orden cronológico, de cualquier tamaño).

    Valida cada trozo (Art. 13): columnas presentes con dtype canónico y ``open_time_ms``
    sin nulos y estrictamente creciente, también entre trozos.

    Args:
        strict_columns: columnas del ``content_hash``, en orden (incluye ``open_time_ms``).
        informational_columns: columnas hasheadas aparte (no afectan al ``content_hash``).
        probe_rows: índices de fila cuyo ``close`` se captura (sondas RF-6).
    """

    def __init__(
        self,
        strict_columns: Sequence[str],
        informational_columns: Sequence[str] = (),
        probe_rows: Sequence[int] = (),
    ) -> None:
        self._strict = tuple(strict_columns)
        self._informational = tuple(informational_columns)
        self._columns = (*self._strict, *self._informational)
        if TIME_COLUMN not in self._strict:
            msg = f"{TIME_COLUMN} debe ser columna estricta"
            raise ValueError(msg)
        self._totals = {c: _ColumnDigest(c) for c in self._columns}
        self._months: dict[str, dict[str, _ColumnDigest]] = {}
        self._probe_rows = sorted(set(probe_rows))
        self._probes: list[ProbeValue] = []
        self._rows = 0
        self._first: int | None = None
        self._last: int | None = None

    def update(self, frame: pl.DataFrame) -> None:
        """Añade un trozo de filas posteriores a las ya procesadas."""
        if frame.height == 0:
            return
        check_chunk(frame, self._columns, self._last)
        self._capture_probes(frame)
        for column in self._columns:
            self._totals[column].update(frame[column])
        months = pl.from_epoch(frame[TIME_COLUMN], time_unit="ms").dt.strftime("%Y-%m")
        labelled = frame.select(self._strict).with_columns(months.alias("__month"))
        for part in labelled.partition_by("__month", maintain_order=True, as_dict=False):
            month = str(part["__month"][0])
            digests = self._months.setdefault(month, {c: _ColumnDigest(c) for c in self._strict})
            for column in self._strict:
                digests[column].update(part[column])
        if self._first is None:
            self._first = int(frame[TIME_COLUMN][0])
        self._last = int(frame[TIME_COLUMN][-1])
        self._rows += frame.height

    def _capture_probes(self, frame: pl.DataFrame) -> None:
        end = self._rows + frame.height
        wanted = [r for r in self._probe_rows if self._rows <= r < end]
        if not wanted:
            return
        if PROBE_COLUMN not in frame.columns:
            msg = f"Columna de sondas ausente: {PROBE_COLUMN}"
            raise DatasetDataError(msg)
        local = [r - self._rows for r in wanted]
        closes = frame[PROBE_COLUMN].gather(local)
        bits = float32_bits(closes)
        for i, row in enumerate(wanted):
            self._probes.append(
                ProbeValue(
                    row=row,
                    open_time_ms=int(frame[TIME_COLUMN][local[i]]),
                    close_bits=None if closes[i] is None else int(bits[i]),
                )
            )

    def result(self) -> HashResult:
        """Hashes acumulados (puede llamarse en cualquier momento)."""
        column_hashes = {c: _prefixed(self._totals[c].digest()) for c in self._columns}
        return HashResult(
            row_count=self._rows,
            first_open_time_ms=self._first,
            last_open_time_ms=self._last,
            content_hash=_content_hash(self._totals[c] for c in self._strict),
            column_hashes=column_hashes,
            month_hashes={
                month: _content_hash(digests[c] for c in self._strict)
                for month, digests in self._months.items()
            },
            informational={c: column_hashes[c] for c in self._informational},
            probes=tuple(self._probes),
        )


def hash_frame(
    frame: pl.DataFrame,
    strict_columns: Sequence[str],
    informational_columns: Sequence[str] = (),
    probe_rows: Sequence[int] = (),
) -> HashResult:
    """Hash ``ftds-v1`` de un ``DataFrame`` completo (equivale a procesarlo por trozos)."""
    hasher = ItemHasher(strict_columns, informational_columns, probe_rows)
    hasher.update(frame)
    return hasher.result()
