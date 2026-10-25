"""T4.1–T4.2: capa de servicio compartida (orquestación + errores tipados + métricas RF-8)."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.schemas.data_viewer import DatasetIdentifier, ExportRequest
from structlog.testing import capture_logs

from finances_downloader.ft_data_viewer import service
from finances_downloader.ft_data_viewer.errors import (
    DatasetNotFoundError,
    InvalidArgumentError,
    NoDataInRangeError,
)

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv, write_dataset

BTC = DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")
NOPE = DatasetIdentifier(exchange="binance", market_type="spot", symbol="NOPE", timeframe="1h")
START = JAN_2024_MS + 30 * 24 * HOUR_MS


def _req(ident: DatasetIdentifier = BTC, **fields: Any) -> ExportRequest:
    return ExportRequest(dataset_id=ident, **fields)


def test_make_identifier_normalizes_and_validates() -> None:
    ident = service.make_identifier("Binance", "spot", "btcusdt", "1h")
    assert ident == BTC
    with pytest.raises(InvalidArgumentError, match="market_type"):
        service.make_identifier("binance", "options", "BTCUSDT", "1h")
    with pytest.raises(InvalidArgumentError, match="timeframe"):
        service.make_identifier("binance", "spot", "BTCUSDT", "7h")


def test_parse_columns() -> None:
    assert service.parse_columns(None) is None
    assert service.parse_columns(" open, close ,volume") == ["open", "close", "volume"]
    with pytest.raises(InvalidArgumentError):
        service.parse_columns(" , ")


def test_list_datasets_filters(parquet_dir: Path, btc_dataset: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(5), market_type="futures", symbol="ETHUSDT")
    assert service.list_datasets().total_count == 2
    eth = service.list_datasets(symbol="ethusdt")
    assert [d.id.symbol for d in eth.datasets] == ["ETHUSDT"]
    assert eth.total_rows == 5
    assert service.list_datasets(market_type="spot").total_count == 1
    assert service.list_datasets(exchange="BINANCE", timeframe="1h").total_count == 2
    assert service.list_datasets(exchange="kraken").datasets == []


def test_list_datasets_empty_is_not_error(parquet_dir: Path) -> None:
    res = service.list_datasets()
    assert res.total_count == 0
    assert res.datasets == []


def test_list_datasets_logs_metrics(btc_dataset: Path) -> None:
    with capture_logs() as logs:
        service.list_datasets()
    (event,) = [e for e in logs if e["event"] == "datasets_listed"]
    assert event["component"] == "ft_data_viewer"
    assert event["datasets_found"] == 1
    assert event["scan_duration_ms"] >= 0
    assert event["rows_scanned"] == 48
    assert event["bytes_read"] > 0


def test_get_summary_ok_and_metrics(btc_dataset: Path) -> None:
    with capture_logs() as logs:
        s = service.get_summary(BTC)
    assert s.row_count == 48
    assert s.date_range == (START, START + 47 * HOUR_MS)
    (event,) = [e for e in logs if e["event"] == "dataset_operation_complete"]
    assert event["operation"] == "summary"
    assert event["dataset_id"] == "binance/spot/BTCUSDT/1h"
    assert event["rows_scanned"] == 48
    assert event["bytes_read"] > 0
    assert event["duration_ms"] >= 0


@pytest.mark.parametrize(
    "call",
    [
        lambda: service.get_summary(NOPE),
        lambda: service.get_rows(NOPE),
        lambda: service.get_gaps(NOPE),
        lambda: service.get_anomalies(NOPE),
        lambda: service.export_data(_req(NOPE), stream=io.BytesIO()),
        lambda: service.compare_datasets_service(NOPE, BTC),
        lambda: service.compare_datasets_service(BTC, NOPE),
    ],
)
def test_not_found_raises_typed_error(btc_dataset: Path, call: Callable[[], object]) -> None:
    with pytest.raises(DatasetNotFoundError):
        call()


def test_get_rows_modes_and_projection(btc_dataset: Path) -> None:
    head = service.get_rows(BTC, "head", 2, ["close"])
    assert head == [{"close": pytest.approx(r["close"])} for r in head]
    assert len(head) == 2
    tail = service.get_rows(BTC, "tail", 1)
    assert tail[0]["open_time_ms"] == START + 47 * HOUR_MS
    assert len(service.get_rows(BTC, "sample", 5, seed=3)) == 5


def test_get_rows_unknown_column_is_invalid(btc_dataset: Path) -> None:
    with pytest.raises(InvalidArgumentError, match="nope"):
        service.get_rows(BTC, "head", 2, ["nope"])


def test_get_rows_sample_capped_by_settings(
    btc_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FT_DATA_VIEWER_SAMPLE_MAX_ROWS", "4")
    assert len(service.get_rows(BTC, "sample", 30, seed=1)) == 4
    assert len(service.get_rows(BTC, "head", 30)) == 30  # el tope solo aplica a sampling


def test_get_gaps_uses_threshold_setting(
    parquet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_dataset(parquet_dir, make_ohlcv(50, drop=[5, 20, 21, 22]))
    assert [g.missing_candles for g in service.get_gaps(BTC)] == [1, 3]
    assert [g.missing_candles for g in service.get_gaps(BTC, min_gap_candles=2)] == [3]
    monkeypatch.setenv("FT_DATA_VIEWER_GAP_THRESHOLD_MS", str(2 * HOUR_MS))
    assert [g.missing_candles for g in service.get_gaps(BTC)] == [3]
    assert service.get_summary(BTC).gaps_detected == 1


def test_get_anomalies_ok(btc_dataset: Path) -> None:
    report = service.get_anomalies(BTC)
    assert report.dataset_id == BTC
    assert report.duplicate_timestamps["count"] == 0
    assert service.get_anomalies(BTC, outlier_method="iqr").price_outliers == {}


def test_export_to_output_dir(btc_dataset: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "exports"
    res = service.export_data(
        _req(
            start_ms=START,
            end_ms=START + 9 * HOUR_MS,
            columns=["open_time_ms", "close"],
            format="csv",
            output_dir=str(out_dir),
        ),
    )
    assert res.rows == 10
    assert res.path is not None
    assert res.path.parent == out_dir
    assert res.path.name == "binance_spot_BTCUSDT_1h.csv"
    back = pl.read_csv(res.path)
    assert back.columns == ["open_time_ms", "close"]
    assert back.height == 10


def test_export_to_stream(btc_dataset: Path) -> None:
    buf = io.BytesIO()
    res = service.export_data(_req(format="jsonl"), stream=buf)
    assert res.rows == 48
    assert res.path is None
    assert len(buf.getvalue().decode().splitlines()) == 48


def test_export_no_data_in_range(btc_dataset: Path) -> None:
    with pytest.raises(NoDataInRangeError):
        service.export_data(_req(start_ms=1, end_ms=2), stream=io.BytesIO())


def test_export_invalid_arguments(btc_dataset: Path, parquet_dir: Path) -> None:
    with pytest.raises(InvalidArgumentError, match="start_ms"):
        service.export_data(_req(start_ms=START + 10, end_ms=START), stream=io.BytesIO())
    with pytest.raises(InvalidArgumentError, match="destino"):
        service.export_data(_req())
    with pytest.raises(InvalidArgumentError, match="destino"):
        service.export_data(_req(output_dir="x"), stream=io.BytesIO())
    with pytest.raises(InvalidArgumentError, match="nope"):
        service.export_data(_req(columns=["nope"]), stream=io.BytesIO())


@pytest.mark.parametrize("sub", ["", "binance", "elsewhere/../binance/x"])
def test_export_refuses_output_inside_parquet_dir(
    btc_dataset: Path, parquet_dir: Path, sub: str
) -> None:
    """ADR DV-01: ft_data_viewer nunca escribe dentro de FT_PARQUET_DIR."""
    before = sorted(p for p in parquet_dir.rglob("*"))
    with pytest.raises(InvalidArgumentError, match="PARQUET_DIR"):
        service.export_data(_req(output_dir=str(parquet_dir / sub)))
    assert sorted(p for p in parquet_dir.rglob("*")) == before


def test_compare_service(parquet_dir: Path, btc_dataset: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(48, start_ms=START, drop=[3]), market_type="futures")
    fut = BTC.model_copy(update={"market_type": "futures"})
    res = service.compare_datasets_service(BTC, fut)
    assert res.row_count_diff == 1
    assert [g.missing_candles for g in res.gaps_only_in_b] == [1]
    ranged = service.compare_datasets_service(BTC, fut, START + 10 * HOUR_MS, START + 20 * HOUR_MS)
    assert ranged.row_count_diff == 0


def test_service_reads_settings_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other_root"
    monkeypatch.setenv("FT_PROJECT_ROOT", str(other))
    write_dataset(FinancesSettings().PARQUET_DIR, make_ohlcv(3))
    assert service.list_datasets().total_rows == 3
    monkeypatch.setenv("FT_PARQUET_DIR", str(tmp_path / "void"))
    assert service.list_datasets().total_count == 0


def test_export_requires_dataset_id_when_called_without_one(tmp_path: Path) -> None:
    request = ExportRequest(output_dir=str(tmp_path / "out"))
    assert request.dataset_id is None
    with pytest.raises(InvalidArgumentError, match="dataset_id"):
        service.export_data(request)
