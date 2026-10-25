"""T5.1–T5.5 y T7.2: CLI Typer end-to-end sobre datasets Parquet temporales."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
import structlog
from typer.testing import CliRunner

from finances_downloader.ft_data_viewer import cli
from finances_downloader.ft_data_viewer.cli import app

from .conftest import HOUR_MS, JAN_2024_MS, make_ohlcv, write_dataset

runner = CliRunner()
DS = ["-e", "binance", "-m", "spot", "-s", "BTCUSDT", "-t", "1h"]
START = JAN_2024_MS + 30 * 24 * HOUR_MS


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Cada invocación reconfigura logging hacia streams de CliRunner que luego se cierran."""
    yield
    logging.basicConfig(force=True, handlers=[logging.NullHandler()])
    structlog.reset_defaults()


def _json(stdout: str) -> object:
    return json.loads(stdout)


# --- T5.1 ------------------------------------------------------------------------------


def test_help_lists_nine_commands() -> None:
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in (
        "list",
        "summary",
        "head",
        "tail",
        "sample",
        "gaps",
        "anomalies",
        "export",
        "compare",
    ):
        assert cmd in res.stdout


# --- RF-1 / hallazgo 1: filtros por símbolo -------------------------------------------


def test_list_filter_by_symbol_json(parquet_dir: Path, btc_dataset: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(5), market_type="futures", symbol="ETHUSDT")
    res = runner.invoke(app, ["list", "-s", "ETHUSDT", "-f", "json"])
    assert res.exit_code == 0, res.output
    data = _json(res.stdout)
    assert isinstance(data, dict)
    assert [d["id"]["symbol"] for d in data["datasets"]] == ["ETHUSDT"]


def test_list_table_and_empty(parquet_dir: Path, btc_dataset: Path) -> None:
    res = runner.invoke(app, ["list"])
    assert res.exit_code == 0
    assert "BTCUSDT" in res.stdout
    empty = runner.invoke(app, ["list", "-s", "NOPE"])
    assert empty.exit_code == 0
    assert "No datasets" in empty.stdout


def test_list_csv_via_global_format(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["--format", "csv", "list"])
    assert res.exit_code == 0, res.output
    df = pl.read_csv(io.StringIO(res.stdout))
    assert df["symbol"].to_list() == ["BTCUSDT"]


# --- T5.4 exit codes (pytest -k cli_exit_codes) ---------------------------------------


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["summary", "-e", "binance", "-m", "spot", "-s", "NOPE", "-t", "1h"], 1),
        (["gaps", "-e", "binance", "-m", "spot", "-s", "NOPE", "-t", "1h"], 1),
        (["summary", "-e", "binance", "-m", "options", "-s", "BTCUSDT", "-t", "1h"], 2),
        (["summary", "-e", "binance", "-m", "spot", "-s", "BTCUSDT", "-t", "7h"], 2),
        (["head", *DS, "--cols", "nope"], 2),
        (["head", *DS, "-n", "0"], 2),
        (["export", *DS, "--start", "not-a-date", "-o", "-"], 2),
        (["export", *DS, "--start", "2001-01-01", "--end", "2001-01-02", "-o", "-"], 3),
        (["summary", *DS], 0),
    ],
)
def test_cli_exit_codes(btc_dataset: Path, args: list[str], code: int) -> None:
    res = runner.invoke(app, args)
    assert res.exit_code == code, res.output


def test_cli_exit_codes_io_error(btc_dataset: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    res = runner.invoke(app, ["export", *DS, "-o", str(blocker / "sub")])
    assert res.exit_code == 4, res.output


def test_cli_exit_codes_output_inside_parquet_dir(btc_dataset: Path, parquet_dir: Path) -> None:
    res = runner.invoke(app, ["export", *DS, "-o", str(parquet_dir / "exports")])
    assert res.exit_code == 2


def test_cli_exit_codes_unexpected_error(
    btc_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(cli.service, "get_summary", boom)
    res = runner.invoke(app, ["summary", *DS])
    assert res.exit_code == 1
    assert "kaput" in res.output


# --- T5.5 --cols y --start/--end ------------------------------------------------------


def test_head_cols_projection_json(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["head", *DS, "-n", "3", "--cols", "close, volume", "-f", "json"])
    assert res.exit_code == 0, res.output
    rows = _json(res.stdout)
    assert isinstance(rows, list)
    assert len(rows) == 3
    assert set(rows[0]) == {"close", "volume"}


def test_tail_and_sample_cols(btc_dataset: Path) -> None:
    tail = runner.invoke(app, ["tail", *DS, "-n", "1", "--cols", "open_time_ms", "-f", "json"])
    assert _json(tail.stdout) == [{"open_time_ms": START + 47 * HOUR_MS}]
    sample = runner.invoke(app, ["sample", *DS, "-n", "5", "--cols", "close", "-f", "json"])
    data = _json(sample.stdout)
    assert isinstance(data, list)
    assert len(data) == 5


def test_export_start_end_parsing(btc_dataset: Path, tmp_path: Path) -> None:
    out = tmp_path / "exp"
    res = runner.invoke(
        app,
        [
            "export",
            *DS,
            "--start",
            "2024-02-01",
            "--end",
            str(START + 30 * HOUR_MS),
            "--cols",
            "open_time_ms,close",
            "-f",
            "csv",
            "-o",
            str(out),
        ],
    )
    assert res.exit_code == 0, res.output
    df = pl.read_csv(out / "binance_spot_BTCUSDT_1h.csv")
    assert df.columns == ["open_time_ms", "close"]
    # 2024-02-01T00:00Z = START + 24h → filas 24..30 inclusive
    assert df["open_time_ms"].to_list() == [START + h * HOUR_MS for h in range(24, 31)]


# --- RF-6: export a stdout --------------------------------------------------------------


def test_export_to_stdout_dash(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["export", *DS, "-f", "csv", "-o", "-", "--cols", "open_time_ms"])
    assert res.exit_code == 0, res.output
    df = pl.read_csv(io.StringIO(res.stdout))
    assert df.height == 48
    assert df.columns == ["open_time_ms"]


def test_export_default_is_stdout(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["export", *DS, "-f", "jsonl"])
    assert res.exit_code == 0, res.output
    assert len(res.stdout.splitlines()) == 48


# --- T5.3 formatos de salida ----------------------------------------------------------


def test_head_csv_uses_write_csv(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["head", *DS, "-n", "2", "-f", "csv"])
    assert res.exit_code == 0
    df = pl.read_csv(io.StringIO(res.stdout))
    assert df.height == 2
    assert "close" in df.columns


def test_head_table(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["head", *DS, "-n", "2", "--cols", "open_time_ms"])
    assert res.exit_code == 0
    assert str(START) in res.stdout


def test_summary_table_and_json(btc_dataset: Path) -> None:
    table = runner.invoke(app, ["summary", *DS])
    assert "Rows" in table.stdout
    assert "48" in table.stdout
    js = _json(runner.invoke(app, ["summary", *DS, "-f", "json"]).stdout)
    assert isinstance(js, dict)
    assert js["row_count"] == 48


def test_gaps_formats(parquet_dir: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(30, drop=[4, 5]))
    js = _json(runner.invoke(app, ["gaps", *DS, "-f", "json"]).stdout)
    assert js == [
        {
            "gap_start_ms": JAN_2024_MS + 4 * HOUR_MS,
            "gap_end_ms": JAN_2024_MS + 5 * HOUR_MS,
            "missing_candles": 2,
            "gap_duration_human": "2h",
        }
    ]
    table = runner.invoke(app, ["gaps", *DS, "--min-gap", "1"])
    assert "2024-01-01 04:00:00" in table.stdout
    none = runner.invoke(app, ["gaps", *DS, "--min-gap", "3"])
    assert "No gaps" in none.stdout
    csv = runner.invoke(app, ["--format", "csv", "gaps", *DS])
    assert pl.read_csv(io.StringIO(csv.stdout))["missing_candles"].to_list() == [2]


def test_anomalies_formats(parquet_dir: Path) -> None:
    df = make_ohlcv(100)
    write_dataset(parquet_dir, pl.concat([df, df.head(2)]))
    js = _json(runner.invoke(app, ["anomalies", *DS, "-f", "json"]).stdout)
    assert isinstance(js, dict)
    assert js["duplicate_timestamps"]["count"] == 2
    table = runner.invoke(app, ["anomalies", *DS, "--outlier-method", "iqr"])
    assert table.exit_code == 0
    assert "Duplicados" in table.stdout
    bad = runner.invoke(app, ["--format", "csv", "anomalies", *DS])
    assert bad.exit_code == 2


def test_compare_formats(parquet_dir: Path, btc_dataset: Path) -> None:
    write_dataset(parquet_dir, make_ohlcv(48, start_ms=START, drop=[2]), market_type="futures")
    args = [
        "compare",
        "--exchange-a",
        "binance",
        "--market-a",
        "spot",
        "--symbol-a",
        "BTCUSDT",
        "--timeframe-a",
        "1h",
        "--exchange-b",
        "binance",
        "--market-b",
        "futures",
        "--symbol-b",
        "BTCUSDT",
        "--timeframe-b",
        "1h",
    ]
    js = _json(runner.invoke(app, [*args, "-f", "json"]).stdout)
    assert isinstance(js, dict)
    assert js["row_count_diff"] == 1
    assert len(js["gaps_only_in_b"]) == 1
    table = runner.invoke(app, [*args, "--start", str(START), "--end", "2024-02-02"])
    assert table.exit_code == 0, table.output
    assert "Row count diff" in table.stdout
    missing = runner.invoke(app, [*args[:-2], "--timeframe-b", "4h"])
    assert missing.exit_code == 1


# --- T5.2 flags globales --------------------------------------------------------------


def test_verbose_and_quiet_set_level(btc_dataset: Path) -> None:
    runner.invoke(app, ["-v", "list"])
    assert logging.getLogger().level == logging.DEBUG
    runner.invoke(app, ["-q", "list"])
    assert logging.getLogger().level == logging.WARNING
    runner.invoke(app, ["list"])
    assert logging.getLogger().level == logging.INFO


def test_log_format_json_emits_json_on_stderr(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["--log-format", "json", "list", "-f", "json"])
    assert res.exit_code == 0
    events = [json.loads(line) for line in res.stderr.splitlines() if line.startswith("{")]
    listed = [e for e in events if e["event"] == "datasets_listed"]
    assert listed
    assert listed[0]["component"] == "ft_data_viewer"
    assert "scan_duration_ms" in listed[0]
    # stdout queda limpio para datos (JSON válido)
    assert isinstance(_json(res.stdout), dict)


def test_global_format_json_and_command_override(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["--format", "json", "summary", *DS])
    js = _json(res.stdout)
    assert isinstance(js, dict)
    assert js["row_count"] == 48
    override = runner.invoke(app, ["--format", "json", "summary", *DS, "-f", "table"])
    assert "Column Statistics" in override.stdout


# --- RF-8: barra de progreso en escaneos (solo modo tabla con logs rich) --------------


@pytest.mark.parametrize(
    ("args", "expects_progress"),
    [
        (["list"], True),
        (["-q", "list"], False),
        (["--log-format", "json", "list"], False),
        (["list", "-f", "json"], False),
    ],
)
def test_list_progress_callback(
    btc_dataset: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], expects_progress: bool
) -> None:
    seen: list[bool] = []
    original = cli.service.list_datasets

    def spy(*a: object, **k: object) -> object:
        seen.append(k.get("on_progress") is not None)
        return original(*a, **k)

    monkeypatch.setattr(cli.service, "list_datasets", spy)
    assert runner.invoke(app, args).exit_code == 0
    assert seen == [expects_progress]


def test_error_message_with_brackets_is_not_markup(btc_dataset: Path) -> None:
    res = runner.invoke(app, ["head", *DS, "--cols", "[bold]x"])
    assert res.exit_code == 2
    assert "[bold]x" in res.output
