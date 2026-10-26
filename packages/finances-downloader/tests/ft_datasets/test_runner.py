"""T3.1 — ``runner``: ``argv``, resolución del ejecutable, entorno, *timeout*, ``stderr`` y
``task_id``; contrato de flags con las CLI reales de los descargadores (``--help``, sin red)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from finances_core.config import FinancesSettings

from finances_downloader.ft_datasets import runner as runner_mod
from finances_downloader.ft_datasets.runner import (
    DOWNLOAD_FLAGS,
    DOWNLOADER_SCRIPTS,
    GLOBAL_FLAGS,
    SubprocessRunner,
    build_argv,
    child_env,
    extract_task_id,
    resolve_executable,
    tail_lines,
)

TASK_ID = "0192f000-0000-7000-8000-000000000001"

FAKE_DOWNLOADER = """\
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
sys.stderr.write(json.dumps({"event": "argv", "argv": sys.argv[1:]}) + "\\n")
sys.stderr.write(json.dumps({"event": "env", "parquet": os.environ.get("FT_PARQUET_DIR")}) + "\\n")
if mode == "ok":
    sys.stderr.write(json.dumps({"event": "task_phase", "task_id": "__TASK_ID__"}) + "\\n")
    print("Task ID | __TASK_ID__")
    sys.exit(0)
if mode == "fail":
    for i in range(60):
        sys.stderr.write(f"error line {i}\\n")
    sys.exit(3)
if mode == "sleep":
    sys.stderr.flush()
    time.sleep(60)
""".replace("__TASK_ID__", TASK_ID)


@pytest.fixture
def fake_runner(tmp_path: Path) -> SubprocessRunner:
    script = tmp_path / "fake_downloader.py"
    script.write_text(FAKE_DOWNLOADER, encoding="utf-8")

    def resolver(name: str) -> list[str] | None:
        return [sys.executable, str(script)] if name in DOWNLOADER_SCRIPTS.values() else None

    return SubprocessRunner(resolver)


def _env(mode: str, parquet: Path) -> dict[str, str]:
    return {**os.environ, "FAKE_MODE": mode, "FT_PARQUET_DIR": str(parquet)}


def test_build_argv_binance_and_yfinance() -> None:
    assert build_argv("binance", "spot", ["BTCUSDT", "ETHUSDT"], ["1m", "1h"], 10, 99) == [
        "ft_market_data_binance",
        "--log-format",
        "json",
        "-q",
        "download",
        "--symbol",
        "BTCUSDT",
        "--symbol",
        "ETHUSDT",
        "--timeframe",
        "1m",
        "--timeframe",
        "1h",
        "--start",
        "10",
        "--end",
        "99",
        "--market",
        "spot",
    ]
    argv = build_argv("yfinance", "index", ["^GSPC"], ["1d"], 0, 5)
    assert argv[0] == "ft_market_data_yfinance"
    assert "--market" not in argv
    assert argv[-4:] == ["--start", "0", "--end", "5"]


def test_resolve_executable_prefers_search_dirs_then_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    name = "ft_fake_tool"
    exe = scripts / (f"{name}.exe" if sys.platform == "win32" else name)
    exe.write_text("", encoding="utf-8")
    assert resolve_executable(name, [scripts]) == str(exe)

    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: f"/on/path/{n}")
    assert resolve_executable("other_tool", [scripts]) == "/on/path/other_tool"

    monkeypatch.setattr(runner_mod.shutil, "which", lambda _n: None)
    assert resolve_executable("other_tool", [scripts]) is None


def test_child_env_points_to_absolute_datasets_dir(tmp_path: Path) -> None:
    settings = FinancesSettings()
    env = child_env(settings, Path("data") / "datasets")
    assert Path(env["FT_PARQUET_DIR"]).is_absolute()
    assert env["FT_PARQUET_DIR"] == str((Path("data") / "datasets").resolve())
    assert env["FT_TASKS_DB_PATH"] == str(settings.TASKS_DB_PATH.resolve())
    assert env["FT_PROJECT_ROOT"] == str(tmp_path.resolve())


def test_exit_zero_extracts_task_id_and_passes_env(
    fake_runner: SubprocessRunner, tmp_path: Path
) -> None:
    argv = build_argv("binance", "spot", ["BTCUSDT"], ["1h"], 0, 1)
    run = fake_runner.run_block(4, argv, _env("ok", tmp_path / "store"), 30)
    assert run.block == 4
    assert run.exit_code == 0
    assert not run.timed_out
    assert run.downloader_task_id == TASK_ID
    joined = "\n".join(run.stderr_tail)
    assert str(tmp_path / "store").replace("\\", "\\\\") in joined
    assert '"--symbol", "BTCUSDT"' in joined


def test_nonzero_exit_keeps_last_50_stderr_lines(
    fake_runner: SubprocessRunner, tmp_path: Path
) -> None:
    argv = build_argv("yfinance", "equity", ["AAPL"], ["1d"], 0, 1)
    run = fake_runner.run_block(0, argv, _env("fail", tmp_path), 30)
    assert run.exit_code == 3
    assert len(run.stderr_tail) == 50
    assert run.stderr_tail[-1] == "error line 59"
    assert run.downloader_task_id is None


def test_timeout_kills_and_reports(fake_runner: SubprocessRunner, tmp_path: Path) -> None:
    argv = build_argv("yfinance", "equity", ["AAPL"], ["1d"], 0, 1)
    run = fake_runner.run_block(0, argv, _env("sleep", tmp_path), 1.5)
    assert run.timed_out
    assert run.exit_code is None
    assert run.duration_ms < 30_000
    assert "Timeout" in run.stderr_tail[-1]


def test_missing_executable_reports_its_name() -> None:
    run = SubprocessRunner(lambda _n: None).run_block(
        0, ["ft_market_data_binance", "download"], dict(os.environ), 5
    )
    assert run.exit_code is None
    assert not run.timed_out
    assert "ft_market_data_binance" in run.stderr_tail[0]


def test_launch_error_is_reported(tmp_path: Path) -> None:
    missing = str(tmp_path / "does-not-exist.exe")
    run = SubprocessRunner(lambda _n: [missing]).run_block(
        0, ["ft_market_data_binance"], dict(os.environ), 5
    )
    assert run.exit_code is None
    assert "ft_market_data_binance" in run.stderr_tail[0]


def test_extract_task_id_from_json_logs_or_stdout_table() -> None:
    stderr = f'not json\n{{"event": "a"}}\n{{"event": "b", "task_id": "{TASK_ID}"}}\n'
    assert extract_task_id(stderr) == TASK_ID
    assert extract_task_id("", f"│ Task ID │ {TASK_ID} │") == TASK_ID
    assert extract_task_id('{"task_id": "nope"}', "") is None
    assert tail_lines("a\n\nb\r\n", 1) == ["b"]


@pytest.mark.parametrize("exchange", sorted(DOWNLOADER_SCRIPTS))
def test_downloader_cli_exposes_every_flag_ft_datasets_uses(exchange: str) -> None:
    """Contrato con la CLI pública (sin red): ``--help`` global y de ``download``."""
    name = DOWNLOADER_SCRIPTS[exchange]
    executable = resolve_executable(name)
    assert executable is not None, f"{name} no está instalado en el entorno"
    env = {**os.environ, "COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"}
    outputs = {}
    for args in ([], ["download"]):
        proc = subprocess.run(
            [executable, *args, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        outputs[tuple(args)] = proc.stdout
    for flag in (f for f in GLOBAL_FLAGS if f.startswith("-")):
        assert flag in outputs[()], (name, flag)
    for flag in DOWNLOAD_FLAGS[exchange]:
        assert flag in outputs[("download",)], (name, flag)
