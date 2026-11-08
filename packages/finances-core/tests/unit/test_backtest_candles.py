"""B2.1: ``backtest.candles.read_candles`` sobre ``indicators_store.source`` (RF-4, RF-14).
Enmienda L0-G1: Soporte para volumen.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import polars as pl
import pytest
from indicators_helpers import BINANCE, HOUR_MS, JAN_30_2024_MS, YFINANCE, ohlcv_frame, write_source
from pydantic import ValidationError

import finances_core.backtest.candles as candles_module
from finances_core.backtest.candles import CANDLE_COLUMNS, Candles, count_data_gaps, read_candles
from finances_core.backtest.errors import EmptyRangeError
from finances_core.config import FinancesSettings
from finances_core.indicators_store.source import (
    InvalidSourceError,
    SourceNotFoundError,
    dataset_dir,
)
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.market_data import OHLCV_SCHEMA


@pytest.fixture
def settings() -> FinancesSettings:
    return FinancesSettings()


def _write_frame(settings: FinancesSettings, df: pl.DataFrame) -> None:
    write_parquet_partitioned(
        df, settings.PARQUET_DIR, exchange=BINANCE.exchange, schema=OHLCV_SCHEMA
    )


def _write_with_price(
    settings: FinancesSettings, column: str, index: int, value: float | None
) -> list[int]:
    times = [JAN_30_2024_MS + i * HOUR_MS for i in range(10)]
    df = ohlcv_frame(BINANCE, times)
    values = df[column].to_list()
    values[index] = value
    df = df.with_columns(pl.Series(column, values, dtype=pl.Float32))
    _write_frame(settings, df)
    return times


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_default_range_returns_whole_dataset_with_canonical_dtypes(
    settings: FinancesSettings,
) -> None:
    times = write_source(settings.PARQUET_DIR, BINANCE, rows=100)

    candles = read_candles(settings, BINANCE)

    assert isinstance(candles, Candles)
    assert candles.frame.columns == list(CANDLE_COLUMNS)
    assert candles.frame.schema["open_time_ms"] == pl.Int64
    for col in ("open", "high", "low", "close", "volume"):
        assert candles.frame.schema[col] == pl.Float32
    assert candles.frame["open_time_ms"].to_list() == times
    assert candles.n_bars == 100
    assert candles.start_ms == times[0]
    assert candles.end_ms == times[-1]
    assert candles.data_gaps == 0


def test_range_is_inclusive_on_both_ends(settings: FinancesSettings) -> None:
    times = write_source(settings.PARQUET_DIR, BINANCE, rows=100)

    candles = read_candles(settings, BINANCE, times[10], times[20])

    assert candles.frame["open_time_ms"].to_list() == times[10:21]
    assert (candles.start_ms, candles.end_ms, candles.n_bars) == (times[10], times[20], 11)


def test_open_bounds_and_bounds_between_bars(settings: FinancesSettings) -> None:
    times = write_source(settings.PARQUET_DIR, BINANCE, rows=100)

    assert (
        read_candles(settings, BINANCE, start_ms=times[95]).frame["open_time_ms"].to_list()
        == times[95:]
    )
    assert (
        read_candles(settings, BINANCE, end_ms=times[4]).frame["open_time_ms"].to_list()
        == times[:5]
    )
    between = read_candles(settings, BINANCE, times[10] + 1, times[12] - 1)
    assert between.frame["open_time_ms"].to_list() == [times[11]]


def test_range_crossing_monthly_partitions(settings: FinancesSettings) -> None:
    # 100 velas 1h desde el 30-ene cruzan a febrero (dos archivos mensuales).
    times = write_source(settings.PARQUET_DIR, BINANCE, rows=100)

    candles = read_candles(settings, BINANCE, times[40], times[60])

    assert candles.n_bars == 21


def test_yfinance_dataset_drops_extra_columns(settings: FinancesSettings) -> None:
    write_source(settings.PARQUET_DIR, YFINANCE, rows=12)

    candles = read_candles(settings, YFINANCE)

    assert candles.frame.columns == list(CANDLE_COLUMNS)
    assert candles.n_bars == 12


def test_missing_dataset_raises_source_not_found(settings: FinancesSettings) -> None:
    write_source(settings.PARQUET_DIR, BINANCE, rows=5)
    other = BINANCE.model_copy(update={"timeframe": "4h"})

    with pytest.raises(SourceNotFoundError):
        read_candles(settings, other)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("close", 0.0),
        ("open", -1.0),
        ("low", None),
        ("high", math.nan),
        ("close", math.inf),
    ],
)
def test_non_positive_null_or_non_finite_price_raises_invalid_source(
    settings: FinancesSettings, column: str, value: float | None
) -> None:
    _write_with_price(settings, column, 3, value)

    with pytest.raises(InvalidSourceError, match=column):
        read_candles(settings, BINANCE)


@pytest.mark.parametrize(
    ("value"),
    [
        -1.0,
        math.nan,
        math.inf,
    ],
)
def test_negative_null_or_non_finite_volume_raises_invalid_source(
    settings: FinancesSettings, value: float | None
) -> None:
    _write_with_price(settings, "volume", 3, value)

    with pytest.raises(InvalidSourceError, match="volume"):
        read_candles(settings, BINANCE)


def test_zero_volume_is_valid(settings: FinancesSettings) -> None:
    _write_with_price(settings, "volume", 3, 0.0)
    candles = read_candles(settings, BINANCE)
    assert candles.frame["volume"][3] == 0.0


def test_unordered_times_raise_invalid_source(settings: FinancesSettings) -> None:
    times = [JAN_30_2024_MS + i * HOUR_MS for i in range(5)]
    times[2], times[3] = times[3], times[2]
    # El writer Hive ordena y deduplica: se escribe el archivo mensual en crudo.
    target = dataset_dir(settings, BINANCE) / "year=2024" / "month=01" / "data.parquet"
    target.parent.mkdir(parents=True)
    ohlcv_frame(BINANCE, times).drop("year", "month").write_parquet(target)

    with pytest.raises(InvalidSourceError):
        read_candles(settings, BINANCE)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (JAN_30_2024_MS + 100 * HOUR_MS, None),
        (None, JAN_30_2024_MS - 1),
        (JAN_30_2024_MS + 1, JAN_30_2024_MS + HOUR_MS - 1),
    ],
)
def test_range_without_candles_raises_empty_range(
    settings: FinancesSettings, start: int | None, end: int | None
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE, rows=100)

    with pytest.raises(EmptyRangeError) as info:
        read_candles(settings, BINANCE, start, end)
    assert (info.value.code, info.value.exit_code, info.value.http_status) == (
        "empty_range",
        2,
        400,
    )


def test_start_after_end_raises_value_error(settings: FinancesSettings) -> None:
    write_source(settings.PARQUET_DIR, BINANCE, rows=10)

    with pytest.raises(ValueError, match="start_ms"):
        read_candles(settings, BINANCE, JAN_30_2024_MS + HOUR_MS, JAN_30_2024_MS)


def test_data_gaps_are_counted_and_not_an_error(settings: FinancesSettings) -> None:
    base = JAN_30_2024_MS
    # Huecos: 1h->3h (2h > 1,5h) y 5h->9h; 3h->4h y 4h->5h son normales.
    offsets = [0, 1, 3, 4, 5, 9, 10]
    times = [base + o * HOUR_MS for o in offsets]
    _write_frame(settings, ohlcv_frame(BINANCE, times))

    candles = read_candles(settings, BINANCE)

    assert candles.n_bars == 7
    assert candles.data_gaps == 2
    # Solo cuentan los huecos dentro del rango pedido.
    assert read_candles(settings, BINANCE, times[2], times[4]).data_gaps == 0


def test_count_data_gaps_threshold_is_strictly_above_one_and_a_half_timeframes() -> None:
    half = HOUR_MS // 2
    times = pl.Series([0, HOUR_MS + half, 2 * HOUR_MS + 2 * half + 1], dtype=pl.Int64)
    # 1,5h exactas no es hueco; 1,5h + 1 ms sí.
    assert count_data_gaps(times, "1h") == 1
    assert count_data_gaps(pl.Series([5], dtype=pl.Int64), "1h") == 0


def test_read_candles_never_writes_under_parquet_dir(settings: FinancesSettings) -> None:
    times = write_source(settings.PARQUET_DIR, BINANCE, rows=100)
    before = _tree_digest(settings.PARQUET_DIR)

    read_candles(settings, BINANCE, times[5], times[50])
    with pytest.raises(EmptyRangeError):
        read_candles(settings, BINANCE, start_ms=times[-1] + 1)

    assert _tree_digest(settings.PARQUET_DIR) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "../../etc"},
        {"exchange": ".."},
        {"timeframe": "1h/../../x"},
        {"symbol": "a\\b"},
        {"exchange": "C:\\Windows"},
    ],
)
def test_read_candles_rejects_path_traversal_components(
    settings: FinancesSettings, overrides: dict[str, str]
) -> None:
    write_source(settings.PARQUET_DIR, BINANCE, rows=5)
    hostile = BINANCE.model_copy(update=overrides)  # DatasetIdentifier no valida rutas

    with pytest.raises(ValidationError):
        read_candles(settings, hostile)


def test_read_candles_rechecks_resolved_path_stays_under_parquet_dir(
    settings: FinancesSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defensa en profundidad: aunque el identificador pase la validación, la ruta
    # resuelta (p. ej. un enlace o un cambio de layout) no puede salir de PARQUET_DIR.
    outside = tmp_path / "outside"
    write_source(outside, BINANCE, rows=5)

    def _escaping_dir(_settings: FinancesSettings, ds: DatasetIdentifier) -> Path:
        return (
            outside
            / ds.exchange
            / f"market_type={ds.market_type}"
            / f"symbol={ds.symbol}"
            / (f"timeframe={ds.timeframe}")
        )

    monkeypatch.setattr(candles_module, "dataset_dir", _escaping_dir)

    with pytest.raises(InvalidSourceError, match="PARQUET_DIR"):
        read_candles(settings, BINANCE)
