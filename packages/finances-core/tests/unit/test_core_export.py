"""T1.3 (ft_indicators): exportación streaming compartida (``finances_core.export``)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import polars as pl
import pytest

from finances_core.export import (
    MEDIA_TYPES,
    ForbiddenOutputDirError,
    stream_export,
    validate_output_dir,
)


def _frame(rows: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series(range(rows), dtype=pl.Int64),
            "rsi_14": pl.Series([float(i) for i in range(rows)], dtype=pl.Float32),
            "talib_cdldoji": pl.Series([0, 100] * (rows // 2) + [0] * (rows % 2), dtype=pl.Int16),
        }
    )


def test_media_types_cover_three_formats() -> None:
    assert set(MEDIA_TYPES) == {"parquet", "csv", "jsonl"}


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_stream_export_to_file(tmp_path: Path, fmt: str) -> None:
    out = tmp_path / "nested" / f"x.{fmt}"
    assert stream_export(_frame(11).lazy(), fmt, out) == 11
    if fmt == "parquet":
        back = pl.read_parquet(out)
        assert back.schema["talib_cdldoji"] == pl.Int16
        assert back.schema["rsi_14"] == pl.Float32
    elif fmt == "csv":
        back = pl.read_csv(out)
    else:
        back = pl.read_ndjson(out)
    assert back["open_time_ms"].to_list() == list(range(11))


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
def test_stream_export_to_stream(fmt: str) -> None:
    buf = io.BytesIO()
    assert stream_export(_frame(4).lazy(), fmt, buf) == 4
    data = buf.getvalue()
    if fmt == "parquet":
        assert data[:4] == b"PAR1"
    elif fmt == "csv":
        lines = data.decode().splitlines()
        assert lines[0] == "open_time_ms,rsi_14,talib_cdldoji"
        assert len(lines) == 5
    else:
        lines = data.decode().splitlines()
        assert len(lines) == 4
        assert json.loads(lines[1])["talib_cdldoji"] == 100


def test_stream_export_rejects_unknown_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Formato"):
        stream_export(_frame(1).lazy(), "xlsx", tmp_path / "a")


def test_validate_output_dir_accepts_outside(tmp_path: Path) -> None:
    out = tmp_path / "exports"
    forbidden = [tmp_path / "data" / "parquet", tmp_path / "data" / "indicators"]
    assert validate_output_dir(out, forbidden) == out.resolve()
    assert validate_output_dir(str(out), forbidden) == out.resolve()


@pytest.mark.parametrize("sub", ["parquet", "parquet/binance/x", "indicators", "indicators/a"])
def test_validate_output_dir_rejects_forbidden(tmp_path: Path, sub: str) -> None:
    forbidden = [tmp_path / "data" / "parquet", tmp_path / "data" / "indicators"]
    with pytest.raises(ForbiddenOutputDirError, match="no puede estar dentro") as exc:
        validate_output_dir(tmp_path / "data" / sub, forbidden)
    assert isinstance(exc.value, ValueError)


def test_validate_output_dir_resolves_relative_traversal(tmp_path: Path) -> None:
    forbidden = [tmp_path / "data" / "parquet"]
    sneaky = tmp_path / "exports" / ".." / "data" / "parquet" / "x"
    with pytest.raises(ForbiddenOutputDirError):
        validate_output_dir(sneaky, forbidden)
