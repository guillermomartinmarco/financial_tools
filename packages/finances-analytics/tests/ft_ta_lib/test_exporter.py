"""T4.2: ``exporter.py`` + ``service.export`` (RF-10) sobre ``build_wide_view``."""

from __future__ import annotations

import io
from pathlib import Path

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.export import ForbiddenOutputDirError
from finances_core.indicators_store.store import IndicatorStore, SeriesNotFoundError
from finances_core.indicators_store.view import ExportValidationError
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import SeriesExportRequest
from finances_core.schemas.market_data import TF_MS_MAP
from finances_core.schemas.ta_lib import TaLibComputeRequest, TaLibSpec

from finances_analytics.ft_ta_lib import exporter, service
from finances_analytics.ft_ta_lib.errors import EmptyExportError, TaLibValidationError

from .conftest import JAN_2024_MS, DatasetFactory

HOUR = TF_MS_MAP["1h"]
ID_COLUMNS = ["exchange", "market_type", "symbol", "timeframe", "open_time_ms"]
FUNCTIONS = ("rsi", "cdlengulfing", "minmaxindex")


async def _computed(
    hive_dataset: DatasetFactory, settings: FinancesSettings, *names: str, rows: int = 300
) -> DatasetIdentifier:
    ds = hive_dataset(rows=rows)
    request = TaLibComputeRequest(datasets=[ds], functions=[TaLibSpec(function=n) for n in names])
    await service.run_compute(service.dispatch_compute(request, settings), settings)
    return ds


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("rsi", "talib_rsi_14"),
        ("RSI", "talib_rsi_14"),
        ("talib_rsi_14", "talib_rsi_14"),
        ("TALIB_RSI_14", "talib_rsi_14"),
        ("rsi:timeperiod=21", "talib_rsi_21"),
        ("bbands:matype=ema", "talib_bbands_20_2_2_ema"),
        ("sma:price=adj_close", "talib_sma_30_price_adj_close"),
        ("cdlengulfing", "talib_cdlengulfing"),
    ],
)
def test_function_token_is_translated_to_series_id(token: str, expected: str) -> None:
    assert exporter.series_id_for(token) == expected


@pytest.mark.parametrize("token", ["rsi_14", "nope:x=1", "rsi:nope=1", "bad id", "talib_"])
def test_invalid_token_is_validation_error(token: str) -> None:
    with pytest.raises(TaLibValidationError):
        exporter.series_id_for(token)


def test_series_ids_are_deduplicated() -> None:
    assert exporter.series_ids(["rsi", "talib_rsi_14", "ema"]) == ["talib_rsi_14", "talib_ema_30"]


def test_export_file_name() -> None:
    ds = DatasetIdentifier.parse("binance/futures/ETHUSDT/1h")
    assert exporter.export_file_name(ds, "csv") == "binance_futures_ETHUSDT_1h_ta_lib.csv"


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
async def test_export_to_file_in_each_format(
    hive_dataset: DatasetFactory, settings: FinancesSettings, tmp_path: Path, fmt: str
) -> None:
    ds = await _computed(hive_dataset, settings, *FUNCTIONS)
    out = tmp_path / "out"
    outcome = service.export(
        ds,
        SeriesExportRequest.model_validate(
            {
                "format": fmt,
                "output_dir": str(out),
                "include_ohlcv": True,
                "series": ["rsi", "talib_cdlengulfing"],
            }
        ),
        settings,
    )
    assert outcome.path == out / f"binance_spot_BTCUSDT_1h_ta_lib.{fmt}"
    assert outcome.rows == 300
    assert outcome.series == ["talib_rsi_14", "talib_cdlengulfing"]
    assert outcome.stale == []
    readers = {"parquet": pl.read_parquet, "csv": pl.read_csv, "jsonl": pl.read_ndjson}
    frame = readers[fmt](outcome.path)
    assert frame.columns == [
        *ID_COLUMNS,
        "open",
        "high",
        "low",
        "close",
        "volume",
        "talib_rsi_14",
        "talib_cdlengulfing",
    ]
    assert frame.height == 300
    assert frame["open_time_ms"].is_sorted()
    assert frame["talib_rsi_14"].null_count() == 14


async def test_parquet_export_keeps_integer_dtypes(
    hive_dataset: DatasetFactory, settings: FinancesSettings, tmp_path: Path
) -> None:
    ds = await _computed(hive_dataset, settings, *FUNCTIONS)
    outcome = service.export(
        ds, SeriesExportRequest(format="parquet", output_dir=str(tmp_path)), settings
    )
    assert outcome.path is not None
    schema = pl.read_parquet_schema(outcome.path)
    assert schema["talib_rsi_14"] == pl.Float32
    assert schema["talib_cdlengulfing"] == pl.Int16
    assert schema["talib_minmaxindex_30_minidx"] == pl.Int64
    assert schema["talib_minmaxindex_30_maxidx"] == pl.Int64
    frame = pl.read_parquet(outcome.path)
    maxidx = frame["talib_minmaxindex_30_maxidx"].drop_nulls()
    assert maxidx.is_in(frame["open_time_ms"].implode()).all()  # índices → open_time_ms


async def test_export_to_stream_with_range(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi", "ema")
    buffer = io.BytesIO()
    outcome = service.export(
        ds,
        SeriesExportRequest(
            format="csv", start_ms=JAN_2024_MS + 10 * HOUR, end_ms=JAN_2024_MS + 20 * HOUR
        ),
        settings,
        stream=buffer,
    )
    assert outcome.path is None
    assert outcome.rows == 10
    frame = pl.read_csv(io.BytesIO(buffer.getvalue()))
    assert frame.height == 10
    assert frame.columns == [*ID_COLUMNS, "talib_ema_30", "talib_rsi_14"]


async def test_export_only_sees_the_ft_ta_lib_namespace(
    hive_dataset: DatasetFactory, settings: FinancesSettings, tmp_path: Path
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi")
    neighbour = IndicatorStore.from_settings(settings, "ft_indicators")
    frame = pl.DataFrame({"open_time_ms": [JAN_2024_MS], "rsi_14": [1.0]})
    neighbour.write_series(ds, "rsi_14", frame, {"rsi_14": pl.Float32})
    outcome = service.export(ds, SeriesExportRequest(output_dir=str(tmp_path)), settings)
    assert outcome.series == ["talib_rsi_14"]
    with pytest.raises(TaLibValidationError):
        service.export(ds, SeriesExportRequest(series=["rsi_14"], output_dir=str(tmp_path)))


async def test_export_errors(
    hive_dataset: DatasetFactory, settings: FinancesSettings, tmp_path: Path
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi", "sma")
    with pytest.raises(SeriesNotFoundError, match="ft_ta_lib compute"):
        service.export(ds, SeriesExportRequest(series=["ema"], output_dir=str(tmp_path)))
    with pytest.raises(EmptyExportError):
        service.export(
            ds,
            SeriesExportRequest(start_ms=JAN_2024_MS + 10_000 * HOUR, output_dir=str(tmp_path)),
        )
    with pytest.raises(ExportValidationError, match="talib_rsi_14"):
        service.export(ds, SeriesExportRequest(stability="drop", output_dir=str(tmp_path)))
    dropped = service.export(
        ds,
        SeriesExportRequest(series=["sma"], stability="drop", output_dir=str(tmp_path / "d")),
    )
    assert dropped.rows == 300 - 29
    with pytest.raises(ForbiddenOutputDirError):
        service.export(ds, SeriesExportRequest(output_dir=str(settings.INDICATORS_DIR)))
    with pytest.raises(ValueError, match="output_dir"):
        service.export(ds, SeriesExportRequest())


def test_export_without_computation_is_not_found(
    hive_dataset: DatasetFactory, settings: FinancesSettings, tmp_path: Path
) -> None:
    ds = hive_dataset()
    with pytest.raises(SeriesNotFoundError, match="ft_ta_lib"):
        service.export(ds, SeriesExportRequest(output_dir=str(tmp_path)), settings)
