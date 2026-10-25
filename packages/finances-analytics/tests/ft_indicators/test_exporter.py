"""T5.2: ``exporter.py`` + ``service.export`` (RF-9) sobre ``build_wide_view``."""

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
from finances_core.schemas.indicators import (
    IndicatorComputeRequest,
    IndicatorSpec,
    SeriesExportRequest,
    Stability,
    StoreEntry,
)
from finances_core.schemas.market_data import TF_MS_MAP

from finances_analytics.ft_indicators import exporter, service
from finances_analytics.ft_indicators.errors import EmptyExportError, IndicatorValidationError

from .conftest import JAN_2024_MS, DatasetFactory

HOUR = TF_MS_MAP["1h"]
ID_COLUMNS = ["exchange", "market_type", "symbol", "timeframe", "open_time_ms"]


async def _computed(
    hive_dataset: DatasetFactory, settings: FinancesSettings, *names: str, rows: int = 300
) -> DatasetIdentifier:
    ds = hive_dataset(rows=rows)
    request = IndicatorComputeRequest(
        datasets=[ds], indicators=[IndicatorSpec.model_validate({"name": n}) for n in names]
    )
    await service.run_compute(service.dispatch_compute(request, settings), settings)
    return ds


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("rsi", "rsi_14"),
        ("RSI", "rsi_14"),
        ("rsi_14", "rsi_14"),
        ("rsi:period=21", "rsi_21"),
        ("bb:std=2.5", "bb_20_2p5"),
        ("macd_12_26_9", "macd_12_26_9"),
        ("obv", "obv"),
        ("sma:period=50,source=adj_close", "sma_50_adj_close"),
    ],
)
def test_indicator_token_is_translated_to_series_id(token: str, expected: str) -> None:
    assert exporter.series_id_for(token) == expected


@pytest.mark.parametrize("token", ["talib_rsi_14", "rsi:period=1", "nope:x=1", "bad id"])
def test_invalid_indicator_token_is_validation_error(token: str) -> None:
    with pytest.raises(IndicatorValidationError):
        exporter.series_id_for(token)


def test_series_ids_are_deduplicated() -> None:
    assert exporter.series_ids(["rsi", "rsi_14", "ema"]) == ["rsi_14", "ema_20"]


def test_export_file_name() -> None:
    ds = DatasetIdentifier.parse("binance/futures/ETHUSDT/1h")
    assert exporter.export_file_name(ds, "csv") == "binance_futures_ETHUSDT_1h_indicators.csv"


@pytest.mark.parametrize("fmt", ["parquet", "csv", "jsonl"])
async def test_export_to_file_in_each_format(
    hive_dataset: DatasetFactory, settings: FinancesSettings, tmp_path: Path, fmt: str
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi", "macd")
    out = tmp_path / "out"
    outcome = service.export(
        ds,
        SeriesExportRequest.model_validate(
            {"format": fmt, "output_dir": str(out), "include_ohlcv": True, "series": ["rsi"]}
        ),
        settings,
    )
    assert outcome.path == out / f"binance_spot_BTCUSDT_1h_indicators.{fmt}"
    assert outcome.rows == 300
    assert outcome.series == ["rsi_14"]
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
        "rsi_14",
    ]
    assert frame.height == 300
    assert frame["open_time_ms"].is_sorted()
    assert frame["rsi_14"].null_count() == 14


async def test_export_to_stream_with_range_and_all_indicators(
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
    frame = pl.read_csv(io.BytesIO(buffer.getvalue()))
    assert frame.height == outcome.rows == 10  # end exclusivo
    assert frame.columns == [*ID_COLUMNS, "ema_20", "rsi_14"]


async def test_stability_flag_and_drop(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = await _computed(hive_dataset, settings, "ema", "sma")
    buffer = io.BytesIO()
    service.export(ds, SeriesExportRequest(format="csv", stability="flag"), settings, stream=buffer)
    flagged = pl.read_csv(io.BytesIO(buffer.getvalue()))
    assert flagged["ema_20_is_stable"].sum() == 300 - 158
    assert flagged["sma_20_is_stable"].sum() == 300 - 19

    buffer = io.BytesIO()
    outcome = service.export(
        ds, SeriesExportRequest(format="csv", stability="drop"), settings, stream=buffer
    )
    dropped = pl.read_csv(io.BytesIO(buffer.getvalue()))
    assert outcome.rows == dropped.height == 300 - 158
    assert dropped["open_time_ms"][0] == JAN_2024_MS + 158 * HOUR


async def test_stability_drop_with_obv_is_validation_error(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = await _computed(hive_dataset, settings, "obv", "rsi")
    with pytest.raises(ExportValidationError, match="obv"):
        service.export(ds, SeriesExportRequest(stability="drop"), settings, stream=io.BytesIO())


async def test_not_computed_indicator_is_not_found(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi")
    with pytest.raises(SeriesNotFoundError, match="compute"):
        service.export(ds, SeriesExportRequest(series=["ema"]), settings, stream=io.BytesIO())
    other = hive_dataset(symbol="ETHUSDT")
    with pytest.raises(SeriesNotFoundError, match="compute"):
        service.export(other, SeriesExportRequest(), settings, stream=io.BytesIO())


async def test_empty_range_is_empty_export(
    hive_dataset: DatasetFactory, settings: FinancesSettings, tmp_path: Path
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi")
    with pytest.raises(EmptyExportError):
        service.export(
            ds,
            SeriesExportRequest(start_ms=JAN_2024_MS + 10_000 * HOUR, output_dir=str(tmp_path)),
            settings,
        )
    assert not list(tmp_path.glob("*.parquet"))


async def test_ft_ta_lib_series_in_same_directory_are_not_exported(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi")
    talib = IndicatorStore(settings.INDICATORS_DIR, "ft_ta_lib")
    frame = pl.DataFrame(
        {
            "open_time_ms": [JAN_2024_MS + i * HOUR for i in range(300)],
            "talib_rsi_14": [50.0] * 300,
        },
        schema={"open_time_ms": pl.Int64, "talib_rsi_14": pl.Float32},
    )
    stats = talib.write_series(ds, "talib_rsi_14", frame, {"talib_rsi_14": pl.Float32})
    talib.update_manifest(
        ds,
        {
            "talib_rsi_14": StoreEntry(
                series_id="talib_rsi_14",
                spec={},
                columns=["talib_rsi_14"],
                column_dtypes={"talib_rsi_14": "Float32"},
                engine_version="ta-lib",
                rows=stats.rows,
                first_ms=stats.first_ms,
                last_ms=stats.last_ms,
                stability=Stability(
                    stable_from_ms=None, by_column={}, rule="", reason="first_non_null"
                ),
                computed_at_ms=0,
            )
        },
    )
    buffer = io.BytesIO()
    service.export(ds, SeriesExportRequest(format="csv"), settings, stream=buffer)
    assert pl.read_csv(io.BytesIO(buffer.getvalue())).columns == [*ID_COLUMNS, "rsi_14"]
    with pytest.raises(IndicatorValidationError):
        service.export(
            ds, SeriesExportRequest(series=["talib_rsi_14"]), settings, stream=io.BytesIO()
        )


async def test_output_dir_inside_data_dirs_is_rejected(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi")
    for forbidden in (settings.PARQUET_DIR / "x", settings.INDICATORS_DIR):
        with pytest.raises(ForbiddenOutputDirError):
            service.export(ds, SeriesExportRequest(output_dir=str(forbidden)), settings)


async def test_stale_series_are_reported(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = await _computed(hive_dataset, settings, "rsi", "ema")
    hive_dataset(rows=5, start_ms=JAN_2024_MS + 300 * HOUR, seed=3)
    buffer = io.BytesIO()
    outcome = service.export(ds, SeriesExportRequest(format="csv"), settings, stream=buffer)
    assert outcome.stale == ["ema_20", "rsi_14"]
    assert outcome.rows == 300  # se exporta lo persistido
