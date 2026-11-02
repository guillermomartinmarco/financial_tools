"""T10.1-T10.2: RF-10 lectura de velas por rango (servicio, API y CLI)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finances_core.schemas.data_viewer import CandlesPage, DatasetIdentifier
from typer.testing import CliRunner

from finances_downloader.ft_data_viewer import service
from finances_downloader.ft_data_viewer.api import router
from finances_downloader.ft_data_viewer.cli import app
from finances_downloader.ft_data_viewer.errors import DatasetNotFoundError, InvalidArgumentError

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv, write_dataset

BASE = "/data/datasets/binance/spot/BTCUSDT/1h"
DS = ["-e", "binance", "-m", "spot", "-s", "BTCUSDT", "-t", "1h"]
IDENT = DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")


def ts(i: int) -> int:
    return JAN_2024_MS + i * HOUR_MS


@pytest.fixture
def client() -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(router)
    with TestClient(api, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def dataset(parquet_dir: Path) -> Path:
    return write_dataset(parquet_dir, make_ohlcv(100))


# --- Servicio (T10.1) -----------------------------------------------------------------


def test_range_is_inclusive_and_ascending(dataset: Path) -> None:
    page = service.get_candles(IDENT, start_ms=ts(10), end_ms=ts(14))
    assert [r["open_time_ms"] for r in page.items] == [ts(i) for i in range(10, 15)]
    assert page.count == 5
    assert page.next_after_ms is None
    assert set(page.items[0]) == set(make_ohlcv(1).columns)


def test_open_ended_bounds(dataset: Path) -> None:
    assert service.get_candles(IDENT, start_ms=ts(95)).count == 5
    assert service.get_candles(IDENT, end_ms=ts(2)).count == 3


def test_limit_and_next_after_ms(dataset: Path) -> None:
    page = service.get_candles(IDENT, limit=30)
    assert page.count == 30
    assert page.next_after_ms == ts(29)
    exact = service.get_candles(IDENT, start_ms=ts(0), end_ms=ts(29), limit=30)
    assert exact.count == 30
    assert exact.next_after_ms is None


def test_walking_pages_reproduces_dataset(dataset: Path) -> None:
    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        page = service.get_candles(IDENT, limit=30, after_ms=cursor)
        seen.extend(r["open_time_ms"] for r in page.items)
        pages += 1
        if page.next_after_ms is None:
            break
        cursor = page.next_after_ms
    assert pages == 4
    assert seen == [ts(i) for i in range(100)]


def test_cursor_stable_after_append(parquet_dir: Path, dataset: Path) -> None:
    first = service.get_candles(IDENT, limit=60)
    assert first.next_after_ms == ts(59)
    extra = make_ohlcv(20, start_ms=ts(100))
    part = dataset / "year=2024" / "month=01"
    extra.write_parquet(part / "append.parquet")
    rest = service.get_candles(IDENT, limit=1000, after_ms=first.next_after_ms)
    assert [r["open_time_ms"] for r in rest.items] == [ts(i) for i in range(60, 120)]


def test_after_ms_combined_with_range(dataset: Path) -> None:
    page = service.get_candles(IDENT, start_ms=ts(10), end_ms=ts(20), after_ms=ts(15))
    assert [r["open_time_ms"] for r in page.items] == [ts(i) for i in range(16, 21)]


def test_empty_range_is_not_an_error(dataset: Path) -> None:
    page = service.get_candles(IDENT, start_ms=ts(500), end_ms=ts(600))
    assert page == CandlesPage(items=[], count=0, next_after_ms=None)


def test_columns_projection(dataset: Path) -> None:
    page = service.get_candles(IDENT, cols=["open_time_ms", "close"], limit=2)
    assert all(set(r) == {"open_time_ms", "close"} for r in page.items)


def test_projection_without_timestamp_still_paginates(dataset: Path) -> None:
    page = service.get_candles(IDENT, cols=["close"], limit=2)
    assert all(set(r) == {"close"} for r in page.items)
    assert page.next_after_ms == ts(1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_ms": 10, "end_ms": 5},
        {"cols": ["nope"]},
        {"limit": 0},
        {"limit": 10001},
    ],
)
def test_invalid_arguments(dataset: Path, kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentError):
        service.get_candles(IDENT, **kwargs)


def test_missing_dataset(parquet_dir: Path) -> None:
    with pytest.raises(DatasetNotFoundError):
        service.get_candles(IDENT)


# --- API (T10.2) ----------------------------------------------------------------------


def test_api_page_and_cursor(client: TestClient, dataset: Path) -> None:
    body = client.get(f"{BASE}/candles", params={"limit": 40, "columns": "open_time_ms"}).json()
    assert body["count"] == 40
    assert body["next_after_ms"] == ts(39)
    assert body["items"][0] == {"open_time_ms": ts(0)}
    nxt = client.get(f"{BASE}/candles", params={"after_ms": body["next_after_ms"]}).json()
    assert nxt["count"] == 60
    assert nxt["next_after_ms"] is None


def test_api_empty_range_is_200(client: TestClient, dataset: Path) -> None:
    res = client.get(f"{BASE}/candles", params={"start_ms": ts(900)})
    assert res.status_code == 200
    assert res.json() == {"items": [], "count": 0, "next_after_ms": None}


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"start_ms": 10, "end_ms": 5}, 400),
        ({"columns": "nope"}, 400),
        ({"limit": 10001}, 422),
        ({"limit": 0}, 422),
    ],
)
def test_api_errors(
    client: TestClient, dataset: Path, params: dict[str, int | str], status: int
) -> None:
    assert client.get(f"{BASE}/candles", params=params).status_code == status


def test_api_missing_dataset_404(client: TestClient, parquet_dir: Path) -> None:
    assert client.get(f"{BASE}/candles").status_code == 404


def test_api_rows_unchanged(client: TestClient, dataset: Path) -> None:
    rows = client.get(f"{BASE}/rows", params={"mode": "head", "n": 3}).json()
    assert [r["open_time_ms"] for r in rows] == [ts(0), ts(1), ts(2)]


# --- CLI (T10.2) ----------------------------------------------------------------------


def _cli(args: list[str]) -> Any:
    res = CliRunner().invoke(app, ["candles", *DS, *args])
    return res


def test_cli_json_matches_api(client: TestClient, dataset: Path) -> None:
    res = _cli(
        [
            "--start-ms",
            str(ts(5)),
            "--end-ms",
            str(ts(50)),
            "--limit",
            "7",
            "--cols",
            "open_time_ms,close",
            "-f",
            "json",
        ]
    )
    assert res.exit_code == 0, res.output
    api = client.get(
        f"{BASE}/candles",
        params={"start_ms": ts(5), "end_ms": ts(50), "limit": 7, "columns": "open_time_ms,close"},
    ).json()
    assert json.loads(res.stdout) == api


def test_cli_table_and_empty(dataset: Path) -> None:
    res = _cli(["--limit", "3"])
    assert res.exit_code == 0, res.output
    assert "No rows" not in res.stdout
    assert res.stdout.count(chr(10)) >= 3
    empty = _cli(["--start-ms", str(ts(900))])
    assert empty.exit_code == 0
    assert "No rows" in empty.stdout


def test_cli_exit_codes(dataset: Path, parquet_dir: Path) -> None:
    assert _cli(["--start-ms", "10", "--end-ms", "5"]).exit_code == 2
    assert _cli(["--cols", "nope"]).exit_code == 2
    assert _cli(["--limit", "10001"]).exit_code == 2
    missing = CliRunner().invoke(
        app, ["candles", "-e", "binance", "-m", "spot", "-s", "NOPE", "-t", "1h"]
    )
    assert missing.exit_code == 1
