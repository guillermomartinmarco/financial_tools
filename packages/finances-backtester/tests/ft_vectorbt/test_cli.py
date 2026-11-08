"""V3.1: CLI Typer ``ft_vectorbt`` — comandos ``run`` y ``kinds [--kind] [--check]``.

Comprueba la lista de opciones de RF-6, la ejecución completa (tabla y ``--format json``),
``--request-file``, ``--force`` y la caché, los códigos de salida RF-9 (1 no encontrado, 2
validación, 3 ``FAILED``, 4 IO) y el listado/verificación de estrategias. Reutiliza las
*fixtures* de ``conftest`` (``FT_PROJECT_ROOT`` temporal, *fixture* de contrato como fuente
Hive) y la estrategia sintética de la suite de contrato como estrategia local de prueba.
Sin red.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import typer.main
from finances_core.backtest.errors import OutputOutOfRangeError, StrategyRuntimeError
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import BacktestRequest
from typer.testing import CliRunner

from finances_backtester.ft_vectorbt import cli, service
from tests.contract.engine_contract import SYNTHETIC_DIR, SYNTHETIC_KIND
from tests.contract.fixture import GOLDEN_PARAMS
from tests.ft_vectorbt.conftest import DATASET, create_strategy, make_request, sma_request

runner = CliRunner()

FIXTURE_TRADES = 55  # sma_cross con los parámetros de los golden (B6.1)
BUILTIN_KINDS = [
    "bollinger_reversion",
    "donchian_breakout",
    "ema_cross",
    "high_breakout",
    "macd_trend",
    "momentum_roc",
    "rsi2_connors",
    "rsi_reversion",
    "sma_cross",
    "zscore_reversion",
]
#: UUID v7 válido por forma pero inexistente en la base de estrategias.
UNKNOWN_STRATEGY = "0192f000-0000-7000-8000-0000000000ff"
#: UUID v7 válido por forma pero inexistente en ``tasks.db``.
UNKNOWN_TASK = "0192f000-0000-7000-8000-00000000ffff"
#: run_id SHA-256 válido por forma pero inexistente en el almacén.
UNKNOWN_RUN = "0" * 64


def _make_run(request: BacktestRequest, settings: FinancesSettings) -> str:
    """Ejecuta ``run_sync`` una vez para dejar una corrida completa en disco; devuelve run_id."""
    return service.run_sync(request, settings).run_id


def _rsi_request() -> BacktestRequest:
    """Petición ``rsi_reversion`` (kind y strategy_id distintos de ``sma_cross``)."""
    strategy_id = create_strategy(
        {"kind": "rsi_reversion", "params": GOLDEN_PARAMS["rsi_reversion"]}
    )
    return make_request(strategy_id)


def _dispatch_completed(request: BacktestRequest, settings: FinancesSettings) -> str:
    """Despacha y ejecuta una tarea de la API hasta ``COMPLETED``; devuelve task_id."""
    dispatched = service.dispatch(request, settings)
    asyncio.run(service.run_task(dispatched, settings))
    return dispatched.task_id


def _invoke(*args: str) -> object:
    return runner.invoke(cli.app, list(args))


def _copy_synthetic(settings: FinancesSettings) -> Path:
    """Instala la estrategia sintética de contrato como estrategia local válida."""
    dst = Path(settings.STRATEGIES_DIR) / SYNTHETIC_KIND
    shutil.copytree(SYNTHETIC_DIR / SYNTHETIC_KIND, dst, ignore=shutil.ignore_patterns("__*"))
    return dst


# --------------------------------------------------------------------- ayuda / RF-6


def test_help_lists_run_and_kinds() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "kinds"):
        assert command in result.output


def test_run_help_lists_rf6_options() -> None:
    # `run --help` responds; assert the RF-6 options by introspecting the Click command
    # rather than the Rich-rendered help text, which wraps/elides long option names by
    # terminal width and colour (CI has no TTY and rendered --dataset unmatchable).
    result = runner.invoke(cli.app, ["run", "--help"])
    assert result.exit_code == 0, result.output
    command = typer.main.get_command(cli.app).commands["run"]
    declared = {opt for param in command.params for opt in param.opts}
    for option in (
        "--dataset",
        "--strategy-id",
        "--strategy-version",
        "--start",
        "--end",
        "--initial-cash",
        "--size-fraction",
        "--fees-bps",
        "--slippage-bps",
        "--force",
        "--request-file",
    ):
        assert option in declared


# ------------------------------------------------------------------------ run OK


def test_run_full_table(service_settings: FinancesSettings) -> None:
    request = sma_request()
    result = _invoke("run", "-d", DATASET.path_segment(), "--strategy-id", request.strategy_id)
    assert result.exit_code == 0, result.output
    assert "n_trades" in result.stdout
    assert str(FIXTURE_TRADES) in result.stdout


def test_run_format_json(service_settings: FinancesSettings) -> None:
    request = sma_request()
    result = runner.invoke(
        cli.app,
        [
            "--format",
            "json",
            "run",
            "-d",
            DATASET.path_segment(),
            "--strategy-id",
            request.strategy_id,
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["cached"] is False
    assert body["summary"]["n_trades"] == FIXTURE_TRADES
    assert body["manifest"]["engine"] == "vectorbt"


def test_run_request_file(service_settings: FinancesSettings, tmp_path: Path) -> None:
    request = sma_request()
    path = tmp_path / "req.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    result = runner.invoke(cli.app, ["--format", "json", "run", "--request-file", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["summary"]["n_trades"] == FIXTURE_TRADES


def test_run_request_file_is_mutually_exclusive(
    service_settings: FinancesSettings, tmp_path: Path
) -> None:
    request = sma_request()
    path = tmp_path / "req.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    result = _invoke(
        "run",
        "--request-file",
        str(path),
        "-d",
        DATASET.path_segment(),
        "--strategy-id",
        request.strategy_id,
    )
    assert result.exit_code == 2, result.output
    assert "request-file" in result.stderr


def test_run_cached_exits_0(service_settings: FinancesSettings) -> None:
    request = sma_request()
    args = [
        "--format",
        "json",
        "run",
        "-d",
        DATASET.path_segment(),
        "--strategy-id",
        request.strategy_id,
    ]
    first = runner.invoke(cli.app, args)
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli.app, args)
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["cached"] is True


def test_run_force_recomputes(service_settings: FinancesSettings) -> None:
    request = sma_request()
    base = [
        "--format",
        "json",
        "run",
        "-d",
        DATASET.path_segment(),
        "--strategy-id",
        request.strategy_id,
    ]
    runner.invoke(cli.app, base)
    forced = runner.invoke(cli.app, [*base, "--force"])
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.stdout)["cached"] is False


# ---------------------------------------------------------- run: códigos de salida


def test_run_strategy_not_found_exit_1(service_settings: FinancesSettings) -> None:
    result = _invoke("run", "-d", DATASET.path_segment(), "--strategy-id", UNKNOWN_STRATEGY)
    assert result.exit_code == 1, result.output


def test_run_dataset_not_found_exit_1(service_settings: FinancesSettings) -> None:
    request = sma_request()
    missing = f"binance/spot/ETHUSDT/{DATASET.timeframe}"
    result = _invoke("run", "-d", missing, "--strategy-id", request.strategy_id)
    assert result.exit_code == 1, result.output


def test_run_unknown_kind_exit_2(service_settings: FinancesSettings) -> None:
    strategy_id = create_strategy({"kind": "no_such_kind", "params": {}})
    result = _invoke("run", "-d", DATASET.path_segment(), "--strategy-id", strategy_id)
    assert result.exit_code == 2, result.output


def test_run_invalid_params_exit_2(service_settings: FinancesSettings) -> None:
    strategy_id = create_strategy({"kind": "sma_cross", "params": {"bogus_key": 1}})
    result = _invoke("run", "-d", DATASET.path_segment(), "--strategy-id", strategy_id)
    assert result.exit_code == 2, result.output


def test_run_negative_costs_exit_2(service_settings: FinancesSettings) -> None:
    request = sma_request()
    result = _invoke(
        "run",
        "-d",
        DATASET.path_segment(),
        "--strategy-id",
        request.strategy_id,
        "--fees-bps",
        "-5",
    )
    assert result.exit_code == 2, result.output


def test_run_invalid_range_exit_2(service_settings: FinancesSettings) -> None:
    request = sma_request()
    result = _invoke(
        "run",
        "-d",
        DATASET.path_segment(),
        "--strategy-id",
        request.strategy_id,
        "--start",
        "2024-02-01",
        "--end",
        "2024-01-01",
    )
    assert result.exit_code == 2, result.output


def test_run_empty_range_exit_2(service_settings: FinancesSettings) -> None:
    request = sma_request()
    # Rango válido pero muy posterior a los datos del fixture: no hay velas -> validación (exit 2).
    result = _invoke(
        "run",
        "-d",
        DATASET.path_segment(),
        "--strategy-id",
        request.strategy_id,
        "--start",
        "4102444800000",
        "--end",
        "4102444900000",
    )
    assert result.exit_code == 2, result.output


def test_run_unparseable_start_exit_2(service_settings: FinancesSettings) -> None:
    request = sma_request()
    result = _invoke(
        "run",
        "-d",
        DATASET.path_segment(),
        "--strategy-id",
        request.strategy_id,
        "--start",
        "garbage",
    )
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize(
    "exc",
    [
        StrategyRuntimeError("sma_cross", RuntimeError("signals boom")),
        OutputOutOfRangeError("output_out_of_range: simulado"),
        RuntimeError("vectorbt boom simulado"),
    ],
)
def test_run_failed_exit_3(
    service_settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    request = sma_request()

    def boom(*_: object, **__: object) -> object:
        raise exc

    monkeypatch.setattr(service, "run_sync", boom)
    result = _invoke("run", "-d", DATASET.path_segment(), "--strategy-id", request.strategy_id)
    assert result.exit_code == 3, result.output


def test_run_io_error_exit_4(
    service_settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = sma_request()

    def boom(*_: object, **__: object) -> object:
        raise OSError("disco lleno simulado")

    monkeypatch.setattr(service, "run_sync", boom)
    result = _invoke("run", "-d", DATASET.path_segment(), "--strategy-id", request.strategy_id)
    assert result.exit_code == 4, result.output


# ----------------------------------------------------------------------- kinds


def test_kinds_lists_builtins_in_deterministic_order(service_settings: FinancesSettings) -> None:
    result = runner.invoke(cli.app, ["--format", "json", "kinds"])
    assert result.exit_code == 0, result.output
    listed = [info["kind"] for info in json.loads(result.stdout)]
    assert listed == BUILTIN_KINDS
    assert all(info["valid"] for info in json.loads(result.stdout))


def test_kinds_includes_invalid_local_folder_with_error(
    service_settings: FinancesSettings,
) -> None:
    folder = _copy_synthetic(service_settings)
    readme = (folder / "README.md").read_text(encoding="utf-8")
    (folder / "README.md").write_text(
        readme.replace("## Matemática\n", "### Otra\n"), encoding="utf-8"
    )

    result = runner.invoke(cli.app, ["--format", "json", "kinds"])
    assert result.exit_code == 0, result.output
    infos = json.loads(result.stdout)
    by_kind = {info["kind"]: info for info in infos}
    # Las 3 integradas siguen en orden determinista entre el resto de fichas.
    assert [k for k in (info["kind"] for info in infos) if k in BUILTIN_KINDS] == BUILTIN_KINDS
    synthetic = by_kind[SYNTHETIC_KIND]
    assert synthetic["valid"] is False
    assert synthetic["origin"] == "local"
    assert synthetic["error"]


def test_kinds_filter_by_kind(service_settings: FinancesSettings) -> None:
    result = runner.invoke(cli.app, ["--format", "json", "kinds", "--kind", "sma_cross"])
    assert result.exit_code == 0, result.output
    infos = json.loads(result.stdout)
    assert [info["kind"] for info in infos] == ["sma_cross"]


def test_kinds_check_passes_for_valid_local(service_settings: FinancesSettings) -> None:
    _copy_synthetic(service_settings)
    result = _invoke("kinds", "--check")
    assert result.exit_code == 0, result.output


def test_kinds_check_fails_on_lookahead(service_settings: FinancesSettings) -> None:
    folder = _copy_synthetic(service_settings)
    logic = folder / "logic.py"
    original = (
        "        t = pl.int_range(pl.len(), dtype=pl.Int64)\n"
        "        return candles.select(\n"
        "            position_from_events(\n"
        "                ((t * 7) % params.entry_stride) == 0,\n"
        "                ((t * 5 + 1) % params.exit_stride) == 0,\n"
        "            )\n"
        "        ).to_series()\n"
    )
    text = logic.read_text(encoding="utf-8")
    assert original in text
    lookahead = (
        '        future = candles["close"].shift(-1)\n'
        '        up = (future > candles["close"]).fill_null(False)\n'
        '        down = (future < candles["close"]).fill_null(False)\n'
        "        return pl.select(position_from_events(up, down)).to_series()\n"
    )
    logic.write_text(text.replace(original, lookahead), encoding="utf-8")

    result = _invoke("kinds", "--check")
    assert result.exit_code == 2, result.output
    assert SYNTHETIC_KIND in result.stderr


def test_kinds_check_fails_on_incomplete_readme(service_settings: FinancesSettings) -> None:
    folder = _copy_synthetic(service_settings)
    readme = (folder / "README.md").read_text(encoding="utf-8")
    (folder / "README.md").write_text(
        readme.replace("## Matemática\n", "### Otra\n"), encoding="utf-8"
    )

    result = _invoke("kinds", "--check")
    assert result.exit_code == 2, result.output
    assert SYNTHETIC_KIND in result.stderr


# ---------------------------------------------------------------- V3.2: runs


def test_help_lists_v32_commands(service_settings: FinancesSettings) -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("runs", "show", "trades", "equity", "status", "result"):
        assert command in result.output


def test_runs_lists_stored_runs(service_settings: FinancesSettings) -> None:
    sma_run = _make_run(sma_request(), service_settings)
    rsi_run = _make_run(_rsi_request(), service_settings)
    result = runner.invoke(cli.app, ["--format", "json", "runs"])
    assert result.exit_code == 0, result.output
    listed = {item["run_id"] for item in json.loads(result.stdout)["items"]}
    assert {sma_run, rsi_run} <= listed


def test_runs_filter_by_strategy_id(service_settings: FinancesSettings) -> None:
    request = sma_request()
    sma_run = _make_run(request, service_settings)
    _make_run(_rsi_request(), service_settings)
    result = runner.invoke(
        cli.app, ["--format", "json", "runs", "--strategy-id", request.strategy_id]
    )
    assert result.exit_code == 0, result.output
    items = json.loads(result.stdout)["items"]
    assert [item["run_id"] for item in items] == [sma_run]
    assert all(item["strategy_id"] == request.strategy_id for item in items)


def test_runs_cursor_pagination(service_settings: FinancesSettings) -> None:
    first_run = _make_run(sma_request(), service_settings)
    second_run = _make_run(_rsi_request(), service_settings)
    first = runner.invoke(cli.app, ["--format", "json", "runs", "--limit", "1"])
    assert first.exit_code == 0, first.output
    first_page = json.loads(first.stdout)
    assert first_page["count"] == 1
    assert first_page["next_cursor"] is not None
    second = runner.invoke(
        cli.app,
        ["--format", "json", "runs", "--limit", "1", "--cursor", first_page["next_cursor"]],
    )
    assert second.exit_code == 0, second.output
    second_page = json.loads(second.stdout)
    first_ids = {item["run_id"] for item in first_page["items"]}
    second_ids = {item["run_id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {first_run, second_run}


def test_runs_bad_limit_exit_2(service_settings: FinancesSettings) -> None:
    result = runner.invoke(cli.app, ["runs", "--limit", "0"])
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------- V3.2: show


def test_show_existing_run_table(service_settings: FinancesSettings) -> None:
    run_id = _make_run(sma_request(), service_settings)
    result = _invoke("show", run_id)
    assert result.exit_code == 0, result.output
    assert run_id[:12] in result.stdout
    assert str(FIXTURE_TRADES) in result.stdout


def test_show_existing_run_json(service_settings: FinancesSettings) -> None:
    run_id = _make_run(sma_request(), service_settings)
    result = runner.invoke(cli.app, ["--format", "json", "show", run_id])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["manifest"]["run_id"] == run_id
    assert body["summary"]["n_trades"] == FIXTURE_TRADES


def test_show_missing_run_exit_1(service_settings: FinancesSettings) -> None:
    result = _invoke("show", UNKNOWN_RUN)
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------- V3.2: trades / equity


def test_trades_paginate_with_after_ms(service_settings: FinancesSettings) -> None:
    run_id = _make_run(sma_request(), service_settings)
    first = runner.invoke(cli.app, ["--format", "json", "trades", run_id, "--limit", "1"])
    assert first.exit_code == 0, first.output
    first_page = json.loads(first.stdout)
    assert first_page["count"] == 1
    assert first_page["next_after_ms"] is not None
    cursor = first_page["next_after_ms"]
    second = runner.invoke(
        cli.app, ["--format", "json", "trades", run_id, "--limit", "1", "--after-ms", str(cursor)]
    )
    assert second.exit_code == 0, second.output
    second_page = json.loads(second.stdout)
    assert second_page["items"][0]["entry_time_ms"] > cursor


def test_trades_table_formats_time_but_json_keeps_ms(service_settings: FinancesSettings) -> None:
    # Robusto ante el *wrapping* de Rich (obs 9: no asertar el render exacto de la tabla).
    # El formato exacto se cubre en los unit tests de ``format_time_ms``/``format_table_cell``.
    run_id = _make_run(sma_request(), service_settings)
    js = runner.invoke(cli.app, ["--format", "json", "trades", run_id, "--limit", "1"])
    assert js.exit_code == 0, js.output
    entry_ms = json.loads(js.stdout)["items"][0]["entry_time_ms"]
    assert isinstance(entry_ms, int)  # JSON mantiene el ms crudo (contrato de máquina)
    table = runner.invoke(cli.app, ["trades", run_id, "--limit", "1"])
    assert table.exit_code == 0, table.output
    assert str(entry_ms) not in table.stdout  # la tabla humana formatea: el ms crudo no aparece


def test_trades_missing_run_exit_1(service_settings: FinancesSettings) -> None:
    result = _invoke("trades", UNKNOWN_RUN)
    assert result.exit_code == 1, result.output


def test_equity_paginate_with_after_ms(service_settings: FinancesSettings) -> None:
    run_id = _make_run(sma_request(), service_settings)
    first = runner.invoke(cli.app, ["--format", "json", "equity", run_id, "--limit", "1"])
    assert first.exit_code == 0, first.output
    first_page = json.loads(first.stdout)
    assert first_page["count"] == 1
    assert first_page["next_after_ms"] is not None
    cursor = first_page["next_after_ms"]
    second = runner.invoke(
        cli.app, ["--format", "json", "equity", run_id, "--limit", "1", "--after-ms", str(cursor)]
    )
    assert second.exit_code == 0, second.output
    second_page = json.loads(second.stdout)
    assert second_page["items"][0]["open_time_ms"] > cursor


def test_equity_missing_run_exit_1(service_settings: FinancesSettings) -> None:
    result = _invoke("equity", UNKNOWN_RUN)
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------- V3.2: status / result


def test_status_of_dispatched_task(service_settings: FinancesSettings) -> None:
    task_id = _dispatch_completed(sma_request(), service_settings)
    result = runner.invoke(cli.app, ["--format", "json", "status", task_id])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["task_id"] == task_id
    assert body["status"] == "COMPLETED"


def test_status_missing_task_exit_1(service_settings: FinancesSettings) -> None:
    result = _invoke("status", UNKNOWN_TASK)
    assert result.exit_code == 1, result.output


def test_result_of_completed_task(service_settings: FinancesSettings) -> None:
    task_id = _dispatch_completed(sma_request(), service_settings)
    result = runner.invoke(cli.app, ["--format", "json", "result", task_id])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["summary"]["n_trades"] == FIXTURE_TRADES
    assert body["manifest"]["engine"] == "vectorbt"


def test_result_missing_task_exit_1(service_settings: FinancesSettings) -> None:
    result = _invoke("result", UNKNOWN_TASK)
    assert result.exit_code == 1, result.output


def test_result_failed_task_exit_3(
    service_settings: FinancesSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched = service.dispatch(sma_request(), service_settings)

    def boom(*_: object, **__: object) -> object:
        raise RuntimeError("vectorbt boom simulado")

    monkeypatch.setattr(service, "run_sync", boom)
    asyncio.run(service.run_task(dispatched, service_settings))
    result = _invoke("result", dispatched.task_id)
    assert result.exit_code == 3, result.output
