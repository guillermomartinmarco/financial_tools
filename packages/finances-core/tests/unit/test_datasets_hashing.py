"""T2.1 de ``ft_datasets``: hash de contenido canónico ``ftds-v1`` (plan §3.2).

Los datos de los vectores dorados se construyen desde **patrones de bits float32
explícitos** (no desde aritmética): el hash se define sobre los bits almacenados, así que
el hex fijado es válido en cualquier SO/CPU. Una implementación de referencia en Python
puro (``struct``) contrasta el algoritmo de forma independiente.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence

import numpy as np
import polars as pl
import pytest

from finances_core.datasets.errors import DatasetDataError
from finances_core.datasets.hashing import (
    CANONICAL_NAN_BITS,
    INFORMATIONAL_COLUMNS,
    STRICT_COLUMNS,
    HashResult,
    ItemHasher,
    hash_frame,
)

JAN_01 = 1_704_067_200_000  # 2024-01-01T00:00Z
JAN_31_23H = JAN_01 + 30 * 86_400_000 + 23 * 3_600_000
FEB_01 = 1_706_745_600_000  # 2024-02-01T00:00Z
FEB_29_23H = FEB_01 + 28 * 86_400_000 + 23 * 3_600_000

NULL = None
#: Bits por columna (None = nulo). Incluye -0.0, subnormales, ±inf, float32::MAX y NaN.
YF_BITS: dict[str, list[int | None]] = {
    "open": [0x3F800000, 0x80000000, 0x00000001, 0x7F7FFFFF],  # 1.0, -0.0, subnormal, MAX
    "high": [0x7F800000, 0x42F6E979, 0x7FC00000, NULL],  # +inf, 123.456, NaN, nulo
    "low": [0xFF800000, 0x00000000, 0x007FFFFF, 0x3DCCCCCD],  # -inf, 0.0, subnormal, 0.1
    "close": [0x42C80000, 0x7FC00000, 0x42CA0000, 0x42CC0000],  # 100, NaN, 101, 102
    "volume": [NULL, 0x47C35000, 0x00000000, 0x4B189680],  # nulo, 1e5, 0.0, 1e7
    "adj_close": [0x42C00000, 0x42C10000, 0x42C20000, 0x42C30000],  # 96..97.5
}
YF_TIMES = [JAN_01, JAN_31_23H, FEB_01, FEB_29_23H]

GOLDEN_YF_CONTENT = "sha256:d46f9d8ddeb6ea9b88c215535a412a2bc0513e5fb5841ef655c8800e11cb1d26"
GOLDEN_YF_ADJ_CLOSE = "sha256:71a2be7ad4bf77f7d520c50845da18085ceb4eb5452fa8492c21486fa7f9aa07"
GOLDEN_BINANCE_CONTENT = "sha256:a447e7e03eceaa2fe37fafc3ea333c4aa6ee15d4fd009cdb5d6c74669d20897f"


def _f32(bits: Sequence[int | None]) -> pl.Series:
    raw = np.array([0 if b is None else b for b in bits], dtype=np.uint32).view(np.float32)
    series = pl.Series(values=raw, dtype=pl.Float32)
    nulls = [i for i, b in enumerate(bits) if b is None]
    return series.scatter(nulls, None) if nulls else series


def _frame(times: Sequence[int], columns: dict[str, list[int | None]]) -> pl.DataFrame:
    data = {"open_time_ms": pl.Series(values=list(times), dtype=pl.Int64)}
    data |= {name: _f32(bits).alias(name) for name, bits in columns.items()}
    return pl.DataFrame(data)


def _yf_frame(**overrides: list[int | None]) -> pl.DataFrame:
    return _frame(YF_TIMES, YF_BITS | overrides)


def _binance_frame() -> pl.DataFrame:
    times = [JAN_01, JAN_01 + 60_000]
    floats = {
        c: [0x3F800000 + i, 0x40000000 + i]
        for i, c in enumerate(STRICT_COLUMNS["binance"])
        if c not in {"open_time_ms", "trades_count"}
    }
    frame = _frame(times, floats).with_columns(
        pl.Series("trades_count", [42, None], dtype=pl.Int64)
    )
    return frame.select(STRICT_COLUMNS["binance"])


# --- Implementación de referencia independiente (struct) ---------------------------------


def _ref_col(name: str, kind: str, values: Sequence[int | None]) -> bytes:
    stream, nulls = hashlib.sha256(), hashlib.sha256()
    for i, v in enumerate(values):
        if v is None:
            stream.update(b"\0" * (8 if kind == "int64" else 4))
            nulls.update(struct.pack("<Q", i))
        elif kind == "int64":
            stream.update(struct.pack("<q", v))
        else:
            is_nan = (v & 0x7F800000) == 0x7F800000 and (v & 0x007FFFFF) != 0
            stream.update(struct.pack("<I", 0x7FC00000 if is_nan else v))
    header = b"ftds-v1\0col\0" + name.encode() + b"\0" + kind.encode() + b"\0"
    return hashlib.sha256(
        header + struct.pack("<Q", len(values)) + stream.digest() + nulls.digest()
    ).digest()


def _ref_content(columns: Sequence[tuple[str, str, Sequence[int | None]]]) -> str:
    body = b"".join(
        name.encode() + b"\0" + _ref_col(name, kind, vals) for name, kind, vals in columns
    )
    return "sha256:" + hashlib.sha256(b"ftds-v1\0item\0" + body).hexdigest()


def _ref_yf(rows: slice = slice(None)) -> str:
    cols: list[tuple[str, str, Sequence[int | None]]] = [("open_time_ms", "int64", YF_TIMES[rows])]
    cols += [(c, "float32", YF_BITS[c][rows]) for c in STRICT_COLUMNS["yfinance"][1:]]
    return _ref_content(cols)


def _yf_hash(frame: pl.DataFrame) -> HashResult:
    return hash_frame(frame, STRICT_COLUMNS["yfinance"], INFORMATIONAL_COLUMNS["yfinance"])


# --- Vectores dorados ---------------------------------------------------------------------


def test_fixture_bits_are_stored_exactly() -> None:
    frame = _yf_frame()
    for name, bits in YF_BITS.items():
        stored = frame[name].to_numpy().view(np.uint32)
        for i, b in enumerate(bits):
            if b is not None and not (b & 0x7F800000 == 0x7F800000 and b & 0x7FFFFF):
                assert int(stored[i]) == b, (name, i)


def test_strict_and_informational_columns() -> None:
    assert STRICT_COLUMNS["binance"] == (
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
    )
    assert STRICT_COLUMNS["yfinance"] == ("open_time_ms", "open", "high", "low", "close", "volume")
    assert INFORMATIONAL_COLUMNS == {"binance": (), "yfinance": ("adj_close",)}
    assert CANONICAL_NAN_BITS == 0x7FC00000


def test_golden_vector_yfinance() -> None:
    result = hash_frame(_yf_frame(), STRICT_COLUMNS["yfinance"], INFORMATIONAL_COLUMNS["yfinance"])
    assert result.content_hash == _ref_yf()
    assert result.content_hash == GOLDEN_YF_CONTENT
    assert (
        "sha256:" + _ref_col("adj_close", "float32", YF_BITS["adj_close"]).hex()
        == GOLDEN_YF_ADJ_CLOSE
    )
    assert result.informational == {"adj_close": GOLDEN_YF_ADJ_CLOSE}
    assert result.column_hashes["adj_close"] == GOLDEN_YF_ADJ_CLOSE
    assert set(result.column_hashes) == {*STRICT_COLUMNS["yfinance"], "adj_close"}
    assert result.month_hashes == {"2024-01": _ref_yf(slice(0, 2)), "2024-02": _ref_yf(slice(2, 4))}
    assert (result.row_count, result.first_open_time_ms, result.last_open_time_ms) == (
        4,
        JAN_01,
        FEB_29_23H,
    )


def test_golden_vector_binance_with_int64_null() -> None:
    frame = _binance_frame()
    result = hash_frame(frame, STRICT_COLUMNS["binance"])
    cols: list[tuple[str, str, Sequence[int | None]]] = []
    for name in STRICT_COLUMNS["binance"]:
        if name in {"open_time_ms", "trades_count"}:
            cols.append((name, "int64", frame[name].to_list()))
        else:
            cols.append((name, "float32", [int(b) for b in frame[name].to_numpy().view(np.uint32)]))
    assert result.content_hash == _ref_content(cols)
    assert result.content_hash == GOLDEN_BINANCE_CONTENT
    assert result.informational == {}


def test_nan_payloads_hash_equal() -> None:
    quiet = _yf_hash(_yf_frame(close=[0x42C80000, 0x7FC00000, 0x42CA0000, 0x42CC0000]))
    x86 = _yf_hash(_yf_frame(close=[0x42C80000, 0xFFC00000, 0x42CA0000, 0x42CC0000]))
    payload = _yf_hash(_yf_frame(close=[0x42C80000, 0x7FC00123, 0x42CA0000, 0x42CC0000]))
    assert quiet == x86 == payload


def test_zero_and_negative_zero_differ() -> None:
    pos = hash_frame(
        _yf_frame(low=[0xFF800000, 0x00000000, 0x007FFFFF, 0x3DCCCCCD]), ["open_time_ms", "low"]
    )
    neg = hash_frame(
        _yf_frame(low=[0xFF800000, 0x80000000, 0x007FFFFF, 0x3DCCCCCD]), ["open_time_ms", "low"]
    )
    assert pos.content_hash != neg.content_hash


def test_null_differs_from_zero_and_from_nan() -> None:
    zero = hash_frame(_yf_frame(volume=[0, 0x47C35000, 0, 0x4B189680]), ["open_time_ms", "volume"])
    null = hash_frame(_yf_frame(), ["open_time_ms", "volume"])
    nan = hash_frame(
        _yf_frame(volume=[0x7FC00000, 0x47C35000, 0, 0x4B189680]), ["open_time_ms", "volume"]
    )
    assert len({zero.content_hash, null.content_hash, nan.content_hash}) == 3


def test_one_bit_flip_changes_content_column_and_month_only() -> None:
    base = hash_frame(_yf_frame(), STRICT_COLUMNS["yfinance"], INFORMATIONAL_COLUMNS["yfinance"])
    flipped = hash_frame(
        _yf_frame(close=[0x42C80000, 0x7FC00000, 0x42CA0001, 0x42CC0000]),  # fila de febrero
        STRICT_COLUMNS["yfinance"],
        INFORMATIONAL_COLUMNS["yfinance"],
    )
    assert flipped.content_hash != base.content_hash
    changed_cols = {
        c for c in base.column_hashes if base.column_hashes[c] != flipped.column_hashes[c]
    }
    assert changed_cols == {"close"}
    changed_months = {
        m for m in base.month_hashes if base.month_hashes[m] != flipped.month_hashes[m]
    }
    assert changed_months == {"2024-02"}


def test_informational_change_keeps_content_hash() -> None:
    base = _yf_hash(_yf_frame())
    adjusted = _yf_hash(_yf_frame(adj_close=[0x42C00001, 0x42C10000, 0x42C20000, 0x42C30000]))
    assert adjusted.content_hash == base.content_hash
    assert adjusted.informational != base.informational


@pytest.mark.parametrize("chunk", [1, 2, 3, 4])
def test_streaming_by_chunks_equals_one_shot(chunk: int) -> None:
    frame = _yf_frame()
    one_shot = _yf_hash(frame)
    hasher = ItemHasher(STRICT_COLUMNS["yfinance"], INFORMATIONAL_COLUMNS["yfinance"])
    for offset in range(0, frame.height, chunk):
        hasher.update(frame.slice(offset, chunk))
    assert hasher.result() == one_shot


def test_by_month_equals_one_shot() -> None:
    frame = _yf_frame()
    hasher = ItemHasher(STRICT_COLUMNS["yfinance"], INFORMATIONAL_COLUMNS["yfinance"])
    hasher.update(frame.filter(pl.col("open_time_ms") < FEB_01))
    hasher.update(frame.filter(pl.col("open_time_ms") >= FEB_01))
    assert hasher.result() == _yf_hash(frame)


def test_probes_captured_while_streaming() -> None:
    frame = _yf_frame()
    hasher = ItemHasher(STRICT_COLUMNS["yfinance"], probe_rows=(0, 2, 3))
    hasher.update(frame.slice(0, 1))
    hasher.update(frame.slice(1, 3))
    probes = hasher.result().probes
    assert [(p.row, p.open_time_ms, p.close_bits) for p in probes] == [
        (0, JAN_01, 0x42C80000),
        (2, FEB_01, 0x42CA0000),
        (3, FEB_29_23H, 0x42CC0000),
    ]


# --- Errores de datos ---------------------------------------------------------------------


def test_duplicate_open_time_is_data_error() -> None:
    frame = _frame([JAN_01, JAN_01], {"close": [1, 2]})
    with pytest.raises(DatasetDataError, match="open_time_ms"):
        hash_frame(frame, ["open_time_ms", "close"])


def test_unsorted_across_chunks_is_data_error() -> None:
    hasher = ItemHasher(["open_time_ms", "close"])
    hasher.update(_frame([FEB_01], {"close": [1]}))
    with pytest.raises(DatasetDataError, match="open_time_ms"):
        hasher.update(_frame([JAN_01], {"close": [1]}))


def test_null_open_time_is_data_error() -> None:
    frame = pl.DataFrame(
        {"open_time_ms": pl.Series([JAN_01, None], dtype=pl.Int64), "close": _f32([1, 2])}
    )
    with pytest.raises(DatasetDataError, match="open_time_ms"):
        hash_frame(frame, ["open_time_ms", "close"])


@pytest.mark.parametrize(
    ("column", "series"),
    [
        ("close", pl.Series("close", [1.0, 2.0], dtype=pl.Float64)),
        ("open_time_ms", pl.Series("open_time_ms", [JAN_01, FEB_01], dtype=pl.UInt64)),
    ],
)
def test_non_canonical_dtype_is_data_error(column: str, series: pl.Series) -> None:
    frame = _frame([JAN_01, FEB_01], {"close": [1, 2]}).with_columns(series.alias(column))
    with pytest.raises(DatasetDataError, match=column):
        hash_frame(frame, ["open_time_ms", "close"])


def test_missing_column_is_data_error() -> None:
    frame = _frame([JAN_01], {"close": [1]})
    with pytest.raises(DatasetDataError, match="volume"):
        hash_frame(frame, ["open_time_ms", "close", "volume"])
