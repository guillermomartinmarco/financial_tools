"""B4.2: almacén de resultados de backtest (RF-7, RF-8, RF-9, A11, plan §1.5 y §3.4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq
import pytest

from finances_core.backtest import metrics, results_store
from finances_core.backtest.errors import OutputOutOfRangeError
from finances_core.backtest.identity import run_id as compute_run_id
from finances_core.backtest.results_store import (
    CAGR_NOT_FINITE,
    StoredRun,
    cleanup_orphans,
    find_run,
    list_runs,
    read_equity_page,
    read_manifest,
    read_trades_page,
    run_dir,
    write_run,
)
from finances_core.schemas.backtest import (
    BacktestRequest,
    ResolvedStrategy,
    RunManifest,
    RunSummary,
)

HOUR_MS = 3_600_000
START_MS = 1_700_000_000_000
STRATEGY_ID = "01890a5d-ac96-774b-bcce-b302099a8057"
OTHER_STRATEGY_ID = "01890a5d-ac96-774b-bcce-b302099a8058"
DATASET = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}
RUN_FILES = {"trades.parquet", "equity.parquet", "summary.json", "manifest.json"}


# ------------------------------------------------------------------ construcción
def _strategy(strategy_id: str = STRATEGY_ID) -> ResolvedStrategy:
    return ResolvedStrategy(
        strategy_id=strategy_id,
        name="alpha",
        version=3,
        hash="a" * 64,
        kind="sma_cross",
        params={"fast": 2, "slow": 4},
        kind_version="1.2.0",
        code_hash="c" * 64,
        origin="local",
    )


def _equity(n_bars: int) -> pl.DataFrame:
    values = [10_000.0 + 10.0 * i for i in range(n_bars)]
    return pl.DataFrame(
        {
            "open_time_ms": [START_MS + i * HOUR_MS for i in range(n_bars)],
            "equity": values,
            "drawdown": [0.0] * n_bars,
        },
        schema={"open_time_ms": pl.Int64, "equity": pl.Float32, "drawdown": pl.Float32},
    )


def _trades(n_trades: int) -> pl.DataFrame:
    entries = [START_MS + 2 * i * HOUR_MS for i in range(n_trades)]
    return pl.DataFrame(
        {
            "entry_time_ms": entries,
            "exit_time_ms": [t + HOUR_MS for t in entries],
            "side": [1] * n_trades,
            "entry_price": [100.0 + i for i in range(n_trades)],
            "exit_price": [101.0 + i for i in range(n_trades)],
            "size": [1.5] * n_trades,
            "fees": [0.1] * n_trades,
            "pnl": [1.4] * n_trades,
            "return": [0.01] * n_trades,
            "exit_reason": [0] * n_trades,
        },
        schema={
            "entry_time_ms": pl.Int64,
            "exit_time_ms": pl.Int64,
            "side": pl.Int8,
            "entry_price": pl.Float32,
            "exit_price": pl.Float32,
            "size": pl.Float32,
            "fees": pl.Float32,
            "pnl": pl.Float32,
            "return": pl.Float32,
            "exit_reason": pl.Int8,
        },
    )


def _inputs(  # noqa: PLR0913 - variantes nombradas de una corrida de prueba
    *,
    n_bars: int = 12,
    n_trades: int = 5,
    dataset: dict[str, str] | None = None,
    strategy_id: str = STRATEGY_ID,
    engine: str = "vectorbt",
    created_at_ms: int = 1_800_000_000_000,
    warnings: list[str] | None = None,
) -> tuple[RunManifest, RunSummary, pl.DataFrame, pl.DataFrame]:
    request = BacktestRequest.model_validate(
        {"dataset": dataset or DATASET, "strategy_id": strategy_id}
    )
    strategy = _strategy(strategy_id)
    equity = _equity(n_bars)
    trades = _trades(n_trades)
    end_ms = START_MS + (n_bars - 1) * HOUR_MS
    rid = compute_run_id(
        request,
        strategy,
        start_ms=START_MS,
        end_ms=end_ms,
        data_hash="d" * 64,
        engine=engine,
        engine_version="1.2.0",
    )
    manifest = RunManifest(
        run_id=rid,
        engine=engine,
        engine_version="1.2.0",
        catalog_version="1.0.0",
        result_schema_version="1",
        request=request,
        strategy=strategy,
        start_ms=START_MS,
        end_ms=end_ms,
        n_bars=n_bars,
        data_hash="d" * 64,
        data_gaps=0,
        created_at_ms=created_at_ms,
        duration_ms=42,
        warnings=warnings if warnings is not None else ["data_gaps"],
        python_version="3.12.0",
        libraries={"polars": "1.0.0"},
    )
    summary = metrics.compute(trades, equity, initial_cash=10_000.0, timeframe="1h")
    return manifest, summary, trades, equity


def _write(results: Path, **kwargs: Any) -> StoredRun:
    force = kwargs.pop("force", False)
    manifest, summary, trades, equity = _inputs(**kwargs)
    return write_run(
        results, manifest=manifest, summary=summary, trades=trades, equity=equity, force=force
    )


def _dataset_dir(results: Path) -> Path:
    return results / "binance" / "market_type=spot" / "symbol=BTCUSDT" / "timeframe=1h"


def _payload_bytes(path: Path) -> dict[str, bytes]:
    return {name: (path / name).read_bytes() for name in sorted(RUN_FILES - {"manifest.json"})}


# ------------------------------------------------------------------------ layout
def test_write_run_follows_rf9_layout(tmp_path: Path) -> None:
    stored = _write(tmp_path)
    expected = _dataset_dir(tmp_path) / stored.manifest.run_id
    assert stored.written is True
    assert stored.path == expected
    assert stored.path == run_dir(tmp_path, stored.manifest.request.dataset, stored.manifest.run_id)
    assert {p.name for p in expected.iterdir()} == RUN_FILES
    assert [p.name for p in _dataset_dir(tmp_path).iterdir()] == [stored.manifest.run_id]


def test_manifest_contains_rf9_strategy_identity_fields(tmp_path: Path) -> None:
    stored = _write(tmp_path)
    raw = json.loads((stored.path / "manifest.json").read_text(encoding="utf-8"))
    assert raw["kind_version"] == "1.2.0"
    assert raw["strategy_code_hash"] == "c" * 64
    assert raw["strategy_origin"] == "local"
    assert raw["strategy"]["kind_version"] == "1.2.0"
    assert raw["catalog_version"] == "1.0.0"
    assert raw["result_schema_version"] == "1"
    assert read_manifest(stored.path) == stored.manifest


def test_manifest_is_written_last_and_directory_installed_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_text = results_store._write_text
    real_parquet = results_store._write_parquet
    real_rename = results_store.os.rename

    def spy_text(path: Path, text: str) -> None:
        order.append(path.name)
        real_text(path, text)

    def spy_parquet(frame: pl.DataFrame, path: Path) -> None:
        order.append(path.name)
        real_parquet(frame, path)

    def spy_rename(src: Any, dst: Any) -> None:
        if ".tmp-" in Path(src).name:
            assert {p.name for p in Path(src).iterdir()} == RUN_FILES
            order.append("rename")
        real_rename(src, dst)

    monkeypatch.setattr(results_store, "_write_text", spy_text)
    monkeypatch.setattr(results_store, "_write_parquet", spy_parquet)
    monkeypatch.setattr(results_store.os, "rename", spy_rename)
    _write(tmp_path)
    assert order[-2:] == ["manifest.json", "rename"]
    assert set(order[:-2]) == RUN_FILES - {"manifest.json"}


def test_failure_before_install_leaves_no_result_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_text = results_store._write_text

    def failing(path: Path, text: str) -> None:
        if path.name == "manifest.json":
            raise OSError("disco lleno")
        real_text(path, text)

    monkeypatch.setattr(results_store, "_write_text", failing)
    with pytest.raises(OSError, match="disco lleno"):
        _write(tmp_path)
    assert list(_dataset_dir(tmp_path).iterdir()) == []


# ------------------------------------------------------- resumen (Nota B3.2, RF-7)
def test_summary_json_has_effective_range_and_rf7_warnings(tmp_path: Path) -> None:
    stored = _write(tmp_path, warnings=["insufficient_history", "data_gaps"])
    raw = json.loads((stored.path / "summary.json").read_text(encoding="utf-8"))
    assert raw["start_ms"] == START_MS
    assert raw["end_ms"] == START_MS + 11 * HOUR_MS
    assert raw["warnings"] == ["insufficient_history", "data_gaps"]
    assert raw["initial_cash"] == 10_000.0
    assert raw["n_bars"] == 12
    assert stored.summary.warnings == stored.manifest.warnings


def test_null_cagr_adds_cagr_not_finite_warning_once(tmp_path: Path) -> None:
    manifest, summary, trades, equity = _inputs(warnings=["data_gaps"])
    summary = summary.model_copy(update={"cagr": None})
    stored = write_run(tmp_path, manifest=manifest, summary=summary, trades=trades, equity=equity)
    assert stored.manifest.warnings == ["data_gaps", CAGR_NOT_FINITE]
    assert stored.summary.warnings == ["data_gaps", CAGR_NOT_FINITE]
    raw = json.loads((stored.path / "summary.json").read_text(encoding="utf-8"))
    assert raw["cagr"] is None
    assert raw["warnings"] == ["data_gaps", CAGR_NOT_FINITE]
    manifest_raw = json.loads((stored.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_raw["warnings"] == ["data_gaps", CAGR_NOT_FINITE]


def test_finite_cagr_adds_no_warning(tmp_path: Path) -> None:
    stored = _write(tmp_path, warnings=[])
    assert stored.summary.cagr is not None
    assert stored.summary.warnings == []
    assert stored.manifest.warnings == []


def test_existing_cagr_warning_is_not_duplicated(tmp_path: Path) -> None:
    manifest, summary, trades, equity = _inputs(warnings=[CAGR_NOT_FINITE])
    summary = summary.model_copy(update={"cagr": None})
    stored = write_run(tmp_path, manifest=manifest, summary=summary, trades=trades, equity=equity)
    assert stored.manifest.warnings == [CAGR_NOT_FINITE]


def test_summary_n_bars_mismatch_is_rejected_without_writing(tmp_path: Path) -> None:
    manifest, summary, trades, equity = _inputs()
    summary = summary.model_copy(update={"n_bars": 99})
    with pytest.raises(ValueError, match="n_bars"):
        write_run(tmp_path, manifest=manifest, summary=summary, trades=trades, equity=equity)
    assert not _dataset_dir(tmp_path).exists()


def test_equity_range_mismatch_is_rejected_without_writing(tmp_path: Path) -> None:
    manifest, summary, trades, equity = _inputs()
    manifest = manifest.model_copy(update={"start_ms": START_MS - HOUR_MS})
    with pytest.raises(ValueError, match="rango"):
        write_run(tmp_path, manifest=manifest, summary=summary, trades=trades, equity=equity)
    assert not _dataset_dir(tmp_path).exists()


def test_non_canonical_tables_are_rejected_without_writing(tmp_path: Path) -> None:
    manifest, summary, trades, equity = _inputs()
    with pytest.raises(OutputOutOfRangeError):
        write_run(
            tmp_path,
            manifest=manifest,
            summary=summary,
            trades=trades,
            equity=equity.with_columns(pl.col("equity").cast(pl.Int64)),
        )
    assert not _dataset_dir(tmp_path).exists()


# --------------------------------------------------------- Parquet reproducible
def test_parquet_uses_zstd(tmp_path: Path) -> None:
    stored = _write(tmp_path)
    for name in ("trades.parquet", "equity.parquet"):
        meta = pq.ParquetFile(stored.path / name).metadata
        codecs = {
            meta.row_group(g).column(c).compression
            for g in range(meta.num_row_groups)
            for c in range(meta.num_columns)
        }
        assert codecs == {"ZSTD"}, name


def test_rewrite_elsewhere_later_is_byte_identical(tmp_path: Path) -> None:
    first = _write(tmp_path / "a")
    time.sleep(1.1)  # cualquier metadato dependiente del tiempo cambiaría los bytes
    second = _write(tmp_path / "b", created_at_ms=1_900_000_000_000)
    assert _payload_bytes(first.path) == _payload_bytes(second.path)


def test_stored_tables_roundtrip_with_canonical_dtypes(tmp_path: Path) -> None:
    manifest, _, trades, equity = _inputs()
    stored = _write(tmp_path)
    assert pl.read_parquet(stored.path / "trades.parquet").equals(trades)
    assert pl.read_parquet(stored.path / "equity.parquet").equals(equity)
    assert stored.manifest.run_id == manifest.run_id


# ------------------------------------------------------------- idempotencia/force
def test_second_write_without_force_returns_existing(tmp_path: Path) -> None:
    first = _write(tmp_path)
    before = (first.path / "manifest.json").read_bytes()
    second = _write(tmp_path, created_at_ms=1_900_000_000_000)
    assert second.written is False
    assert second.manifest == first.manifest
    assert (first.path / "manifest.json").read_bytes() == before


def _install_concurrently(results: Path) -> tuple[Path, bytes]:
    """Prepares another writer's complete result; returns ``(final, manifest bytes)``."""
    other = _write(results / "other")
    final = results / other.path.relative_to(results / "other")
    return final, (other.path / "manifest.json").read_bytes()


def test_non_force_writer_never_moves_aside_a_complete_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another writer installs its complete result right after this writer found none.
    final, other_manifest = _install_concurrently(tmp_path)
    other_dir = tmp_path / "other" / final.relative_to(tmp_path)
    real_move_aside = results_store._move_aside
    moved_complete: list[Path] = []

    def racing_move_aside(path: Path) -> Path | None:
        if path == final and not final.exists():
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(other_dir, final)  # noqa: PTH104 - the other writer installs
        if results_store._load(path) is not None:
            moved_complete.append(path)
        return real_move_aside(path)

    monkeypatch.setattr(results_store, "_move_aside", racing_move_aside)
    stored = _write(tmp_path, created_at_ms=1_900_000_000_000)

    assert moved_complete == []
    if (final / "manifest.json").read_bytes() == other_manifest:
        assert stored.written is False
    assert results_store.read_manifest(final) == stored.manifest
    assert not list(final.parent.glob(f"{final.name}.*"))


def test_result_installed_before_rename_is_kept_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final, other_manifest = _install_concurrently(tmp_path)
    other_dir = tmp_path / "other" / final.relative_to(tmp_path)
    real_rename = results_store._rename

    def racing_rename(src: Path, dst: Path) -> None:
        if dst == final and not final.exists():
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(other_dir, final)  # noqa: PTH104 - the other writer installs first
        real_rename(src, dst)

    monkeypatch.setattr(results_store, "_rename", racing_rename)
    stored = _write(tmp_path, created_at_ms=1_900_000_000_000)

    assert stored.written is False
    assert (final / "manifest.json").read_bytes() == other_manifest
    assert not list(final.parent.glob(f"{final.name}.*"))


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_final_with_broken_summary_is_replaced_without_force(tmp_path: Path, damage: str) -> None:
    first = _write(tmp_path)
    summary = first.path / "summary.json"
    if damage == "missing":
        summary.unlink()
    else:
        summary.write_text("{}", encoding="utf-8")

    second = _write(tmp_path, created_at_ms=1_900_000_000_000)

    assert second.written is True
    assert results_store.read_summary(second.path) == second.summary
    assert second.manifest.created_at_ms == 1_900_000_000_000
    assert not list(first.path.parent.glob(f"{first.path.name}.*"))


def test_force_replaces_atomically_with_identical_content(tmp_path: Path) -> None:
    first = _write(tmp_path)
    payload = _payload_bytes(first.path)
    second = _write(tmp_path, created_at_ms=1_900_000_000_000, force=True)
    assert second.written is True
    assert second.path == first.path
    assert _payload_bytes(second.path) == payload
    assert read_manifest(second.path) == second.manifest
    assert second.manifest.created_at_ms == 1_900_000_000_000
    assert [p.name for p in _dataset_dir(tmp_path).iterdir()] == [first.manifest.run_id]


def test_directory_without_manifest_is_ignored_and_cleaned_on_recompute(tmp_path: Path) -> None:
    manifest, *_ = _inputs()
    partial = run_dir(tmp_path, manifest.request.dataset, manifest.run_id)
    partial.mkdir(parents=True)
    (partial / "trades.parquet").write_bytes(b"incompleto")
    assert read_manifest(partial) is None
    assert find_run(tmp_path, manifest.run_id) is None
    assert list_runs(tmp_path).items == []

    stored = _write(tmp_path)
    assert stored.written is True
    assert {p.name for p in partial.iterdir()} == RUN_FILES
    assert [p.name for p in _dataset_dir(tmp_path).iterdir()] == [manifest.run_id]


def test_manifest_with_other_run_id_or_invalid_json_is_ignored(tmp_path: Path) -> None:
    stored = _write(tmp_path)
    impostor = _dataset_dir(tmp_path) / ("e" * 64)
    impostor.mkdir()
    for name in RUN_FILES:
        (impostor / name).write_bytes((stored.path / name).read_bytes())
    foreign = _dataset_dir(tmp_path) / ("f" * 64)
    foreign.mkdir()
    (foreign / "manifest.json").write_text('{"otro": "resultado"}', encoding="utf-8")
    assert read_manifest(impostor) is None
    assert read_manifest(foreign) is None
    assert find_run(tmp_path, "e" * 64) is None
    assert [item.run_id for item in list_runs(tmp_path).items] == [stored.manifest.run_id]


# ------------------------------------------------------------------- concurrencia
def test_concurrent_threads_same_run_id_leave_one_valid_result(tmp_path: Path) -> None:
    n = 8
    barrier = threading.Barrier(n)
    results: list[StoredRun] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        manifest, summary, trades, equity = _inputs()
        barrier.wait()
        try:
            stored = write_run(
                tmp_path, manifest=manifest, summary=summary, trades=trades, equity=equity
            )
        except BaseException as exc:  # pragma: no cover - el test falla abajo
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(stored)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(r.written for r in results) == 1
    assert len({r.manifest.model_dump_json() for r in results}) == 1
    run = results[0].manifest.run_id
    assert [p.name for p in _dataset_dir(tmp_path).iterdir()] == [run]
    assert read_manifest(_dataset_dir(tmp_path) / run) is not None


def test_concurrent_forced_writes_and_readers_never_see_partial_results(tmp_path: Path) -> None:
    first = _write(tmp_path)
    payload = _payload_bytes(first.path)
    expected_trades = read_trades_page(first.path, limit=100).items
    stop = threading.Event()
    errors: list[str] = []
    seen = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                found = find_run(tmp_path, first.manifest.run_id)
                if found is None:
                    continue
                if found.summary.n_bars != first.summary.n_bars:
                    errors.append("summary distinto")
                if read_trades_page(found.path, limit=100).items != expected_trades:
                    errors.append("trades distintos")
                seen[0] += 1
            except FileNotFoundError:
                continue  # el directorio se apartó durante el reemplazo: no es parcial

    def writer(created: int) -> None:
        for i in range(5):
            _write(tmp_path, created_at_ms=created + i, force=True)

    readers = [threading.Thread(target=reader) for _ in range(2)]
    writers = [
        threading.Thread(target=writer, args=(2_000_000_000_000 + k * 10,)) for k in range(4)
    ]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert errors == []
    assert seen[0] > 0
    final = find_run(tmp_path, first.manifest.run_id)
    assert final is not None
    assert _payload_bytes(final.path) == payload
    assert [p.name for p in _dataset_dir(tmp_path).iterdir()] == [first.manifest.run_id]


def test_force_waits_for_a_reader_holding_a_file_open(tmp_path: Path) -> None:
    # En Windows renombrar un directorio con un archivo abierto da PermissionError: el
    # reemplazo reintenta hasta que el lector lo cierra (en POSIX no hay bloqueo).
    first = _write(tmp_path)
    handle = (first.path / "trades.parquet").open("rb")
    timer = threading.Timer(0.3, handle.close)
    timer.start()
    try:
        second = _write(tmp_path, created_at_ms=1_900_000_000_000, force=True)
    finally:
        timer.join()
        handle.close()
    assert second.written is True
    assert read_manifest(second.path) == second.manifest
    assert [p.name for p in _dataset_dir(tmp_path).iterdir()] == [first.manifest.run_id]


def _child_write(results: str, go_file: str) -> None:
    """Punto de entrada de los procesos hijos del test multiproceso."""
    go = Path(go_file)
    while not go.exists():
        time.sleep(0.005)
    manifest, summary, trades, equity = _inputs()
    stored = write_run(
        Path(results), manifest=manifest, summary=summary, trades=trades, equity=equity
    )
    sys.stdout.write("\n" + ("written" if stored.written else "existing"))


def test_concurrent_processes_same_run_id_leave_one_valid_result(tmp_path: Path) -> None:
    results = tmp_path / "results"
    go = tmp_path / "go"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import test_backtest_results_store as t; t._child_write(sys.argv[2], sys.argv[3])"
    )
    here = str(Path(__file__).parent)
    procs = [
        subprocess.Popen(  # noqa: S603 - intérprete y argumentos controlados por el test
            [sys.executable, "-c", code, here, str(results), str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "FT_PROJECT_ROOT": str(tmp_path)},
        )
        for _ in range(4)
    ]
    time.sleep(1.0)  # que todos los hijos hayan importado antes de la señal
    go.touch()
    outputs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0] * 4, [err for _, err in outputs]
    # structlog escribe en stdout del hijo: el resultado es la última línea.
    assert sorted(out.splitlines()[-1] for out, _ in outputs) == ["existing"] * 3 + ["written"]
    manifest, *_ = _inputs()
    dataset_dir = results / "binance" / "market_type=spot" / "symbol=BTCUSDT" / "timeframe=1h"
    assert [p.name for p in dataset_dir.iterdir()] == [manifest.run_id]
    assert read_manifest(dataset_dir / manifest.run_id) == manifest


# ------------------------------------------------------------- temporales huérfanos
def _orphan(base: Path, name: str, age_s: float) -> Path:
    path = base / name
    path.mkdir(parents=True)
    (path / "trades.parquet").write_bytes(b"x")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_orphan_temps_older_than_24h_are_cleaned(tmp_path: Path) -> None:
    stored = _write(tmp_path)
    base = _dataset_dir(tmp_path)
    rid = stored.manifest.run_id
    old_tmp = _orphan(base, f"{rid}.tmp-{uuid.uuid4().hex}", 25 * 3600)
    old_aside = _orphan(base, f"{'b' * 64}.old-{uuid.uuid4().hex}", 25 * 3600)
    fresh_tmp = _orphan(base, f"{rid}.tmp-{uuid.uuid4().hex}", 60)
    foreign = _orphan(base, "otra_herramienta.tmp-123", 25 * 3600)

    page = list_runs(tmp_path)

    assert [item.run_id for item in page.items] == [rid]
    assert not old_tmp.exists()
    assert not old_aside.exists()
    assert fresh_tmp.exists()  # puede pertenecer a una escritura en curso
    assert foreign.exists()  # FT_RESULTS_DIR es compartido: solo se tocan los nuestros
    assert stored.path.exists()


def test_cleanup_orphans_reports_removed_paths(tmp_path: Path) -> None:
    base = _dataset_dir(tmp_path)
    old = _orphan(base, f"{'a' * 64}.tmp-{uuid.uuid4().hex}", 25 * 3600)
    assert cleanup_orphans(tmp_path) == [old]
    assert cleanup_orphans(tmp_path) == []


# ----------------------------------------------------------------- find/read
def test_find_run_locates_run_across_datasets(tmp_path: Path) -> None:
    _write(tmp_path)
    other = _write(tmp_path, dataset={**DATASET, "market_type": "futures"})
    found = find_run(tmp_path, other.manifest.run_id)
    assert found is not None
    assert found.path == other.path
    assert found.manifest == other.manifest
    assert found.summary == other.summary
    assert found.written is False


@pytest.mark.parametrize("bad", ["", "..", "*", "a" * 63, "A" * 64, "../" + "a" * 61, "a" * 65])
def test_find_run_rejects_malformed_run_id(tmp_path: Path, bad: str) -> None:
    _write(tmp_path)
    assert find_run(tmp_path, bad) is None


def test_find_run_on_missing_results_dir_is_none(tmp_path: Path) -> None:
    assert find_run(tmp_path / "nada", "a" * 64) is None
    assert list_runs(tmp_path / "nada").items == []


# ---------------------------------------------------------------- paginación
def test_trades_cursor_pagination(tmp_path: Path) -> None:
    stored = _write(tmp_path, n_trades=5)
    entries = [START_MS + 2 * i * HOUR_MS for i in range(5)]
    pages = []
    after: int | None = None
    while True:
        page = read_trades_page(stored.path, after_ms=after, limit=2)
        pages.append([row["entry_time_ms"] for row in page.items])
        assert page.count == len(page.items)
        after = page.next_after_ms
        if after is None:
            break
    assert pages == [entries[:2], entries[2:4], entries[4:]]
    first = read_trades_page(stored.path, limit=1).items[0]
    assert set(first) == set(_trades(1).columns)
    assert first["side"] == 1


def test_trades_after_ms_is_exclusive(tmp_path: Path) -> None:
    stored = _write(tmp_path, n_trades=5)
    page = read_trades_page(stored.path, after_ms=START_MS + 2 * HOUR_MS, limit=10)
    assert [row["entry_time_ms"] for row in page.items] == [
        START_MS + 4 * HOUR_MS,
        START_MS + 6 * HOUR_MS,
        START_MS + 8 * HOUR_MS,
    ]
    assert page.next_after_ms is None


def test_exact_full_page_has_no_next_cursor(tmp_path: Path) -> None:
    stored = _write(tmp_path, n_trades=4)
    page = read_trades_page(stored.path, limit=4)
    assert page.count == 4
    assert page.next_after_ms is None


def test_equity_cursor_pagination(tmp_path: Path) -> None:
    stored = _write(tmp_path, n_bars=12)
    page = read_equity_page(stored.path, limit=5)
    assert [row["open_time_ms"] for row in page.items] == [START_MS + i * HOUR_MS for i in range(5)]
    assert page.next_after_ms == START_MS + 4 * HOUR_MS
    rest = read_equity_page(stored.path, after_ms=page.next_after_ms, limit=10_000)
    assert rest.count == 7
    assert rest.next_after_ms is None
    assert set(rest.items[0]) == {"open_time_ms", "equity", "drawdown"}


def test_zero_trades_page_is_empty(tmp_path: Path) -> None:
    stored = _write(tmp_path, n_trades=0)
    page = read_trades_page(stored.path)
    assert (page.items, page.count, page.next_after_ms) == ([], 0, None)


@pytest.mark.parametrize("limit", [0, -1, 10_001])
def test_page_limit_must_be_within_1_and_10000(tmp_path: Path, limit: int) -> None:
    stored = _write(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        read_trades_page(stored.path, limit=limit)
    with pytest.raises(ValueError, match="limit"):
        read_equity_page(stored.path, limit=limit)
    with pytest.raises(ValueError, match="limit"):
        list_runs(tmp_path, limit=limit)


# ------------------------------------------------------------------ list_runs
def _three_runs(results: Path) -> list[StoredRun]:
    return [
        _write(results, created_at_ms=1_000),
        _write(
            results, dataset={**DATASET, "symbol": "ETHUSDT"}, engine="other", created_at_ms=3_000
        ),
        _write(
            results,
            dataset={**DATASET, "exchange": "kraken", "market_type": "futures", "timeframe": "4h"},
            strategy_id=OTHER_STRATEGY_ID,
            created_at_ms=2_000,
        ),
    ]


def test_list_runs_orders_newest_first_with_list_items(tmp_path: Path) -> None:
    runs = _three_runs(tmp_path)
    page = list_runs(tmp_path)
    assert [item.run_id for item in page.items] == [
        runs[1].manifest.run_id,
        runs[2].manifest.run_id,
        runs[0].manifest.run_id,
    ]
    assert page.count == 3
    assert page.next_cursor is None
    item = page.items[2]
    assert item.engine == "vectorbt"
    assert item.strategy_id == STRATEGY_ID
    assert item.strategy_version == 3
    assert item.kind == "sma_cross"
    assert item.dataset.symbol == "BTCUSDT"
    assert item.created_at_ms == 1_000
    assert item.summary == runs[0].summary


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"exchange": "kraken"}, [2]),
        ({"market_type": "spot"}, [1, 0]),
        ({"symbol": "ETHUSDT"}, [1]),
        ({"timeframe": "4h"}, [2]),
        ({"strategy_id": OTHER_STRATEGY_ID}, [2]),
        ({"engine": "other"}, [1]),
        ({"engine": "vectorbt", "market_type": "spot"}, [0]),
        ({"symbol": "NOPE"}, []),
    ],
)
def test_list_runs_filters(tmp_path: Path, filters: dict[str, str], expected: list[int]) -> None:
    runs = _three_runs(tmp_path)
    page = list_runs(tmp_path, **filters)
    assert [item.run_id for item in page.items] == [runs[i].manifest.run_id for i in expected]


def test_list_runs_cursor_pagination(tmp_path: Path) -> None:
    runs = _three_runs(tmp_path)
    first = list_runs(tmp_path, limit=2)
    assert first.count == 2
    assert first.next_cursor is not None
    second = list_runs(tmp_path, limit=2, cursor=first.next_cursor)
    assert [i.run_id for i in first.items + second.items] == [
        runs[1].manifest.run_id,
        runs[2].manifest.run_id,
        runs[0].manifest.run_id,
    ]
    assert second.next_cursor is None


@pytest.mark.parametrize("cursor", ["", "basura", "12:xyz", "abc:" + "a" * 64])
def test_list_runs_rejects_invalid_cursor(tmp_path: Path, cursor: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        list_runs(tmp_path, cursor=cursor)


# ------------------------------------- B4.2 corrección: force fallido y apartados (B1, A1)
@pytest.mark.parametrize("error", [PermissionError(13, "bloqueado"), RuntimeError("fallo")])
def test_failed_force_install_keeps_previous_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    first = _write(tmp_path)
    payload = _payload_bytes(first.path)
    real_rename = os.rename

    def failing_rename(src: Any, dst: Any) -> None:
        if ".tmp-" in Path(src).name:
            raise error
        real_rename(src, dst)

    monkeypatch.setattr(results_store, "_RENAME_ATTEMPTS", 3)
    monkeypatch.setattr(results_store, "_RENAME_WAIT_S", 0.0)
    monkeypatch.setattr(results_store.os, "rename", failing_rename)
    with pytest.raises(type(error)):
        _write(tmp_path, created_at_ms=1_900_000_000_000, force=True)
    monkeypatch.setattr(results_store.os, "rename", real_rename)

    found = find_run(tmp_path, first.manifest.run_id)
    assert found is not None
    assert found.manifest == first.manifest
    assert _payload_bytes(found.path) == payload
    assert [p.name for p in _dataset_dir(tmp_path).iterdir()] == [first.manifest.run_id]


def _hard_kill_leftovers(results: Path) -> tuple[StoredRun, Path]:
    """Estado de un proceso muerto entre los dos ``rename`` de ``force``."""
    first = _write(results)
    aside = first.path.with_name(f"{first.path.name}.old-{uuid.uuid4().hex}")
    os.rename(first.path, aside)  # noqa: PTH104
    tmp = first.path.with_name(f"{first.path.name}.tmp-{uuid.uuid4().hex}")
    tmp.mkdir()
    for name in RUN_FILES:
        (tmp / name).write_bytes((aside / name).read_bytes())
    return first, aside


def test_hard_kill_leftover_is_recovered_by_list_runs(tmp_path: Path) -> None:
    first, aside = _hard_kill_leftovers(tmp_path)
    old = time.time() - 25 * 3600
    os.utime(aside, (old, old))  # aunque parezca huérfano, es el único resultado
    page = list_runs(tmp_path)
    assert [item.run_id for item in page.items] == [first.manifest.run_id]
    assert read_manifest(first.path) == first.manifest
    assert not aside.exists()


def test_hard_kill_leftover_is_recovered_by_find_run(tmp_path: Path) -> None:
    first, aside = _hard_kill_leftovers(tmp_path)
    found = find_run(tmp_path, first.manifest.run_id)
    assert found is not None
    assert found.path == first.path
    assert found.manifest == first.manifest
    assert not aside.exists()


def test_hard_kill_leftover_is_recovered_by_write_run(tmp_path: Path) -> None:
    first, aside = _hard_kill_leftovers(tmp_path)
    again = _write(tmp_path, created_at_ms=1_900_000_000_000)
    assert again.written is False
    assert again.manifest == first.manifest
    assert not aside.exists()


def test_fresh_aside_of_old_result_is_not_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NTFS conserva el mtime al renombrar: el apartado de un resultado de hace > 24 h no
    # debe parecer huérfano mientras el ``force`` que lo apartó sigue en curso.
    first = _write(tmp_path)
    old = time.time() - 25 * 3600
    os.utime(first.path, (old, old))
    seen: list[list[Path]] = []
    real_rename = os.rename

    def spy_rename(src: Any, dst: Any) -> None:
        real_rename(src, dst)
        if ".old-" in Path(dst).name:
            seen.append(cleanup_orphans(tmp_path))  # limpieza concurrente en medio del force
            assert Path(dst).exists()

    monkeypatch.setattr(results_store.os, "rename", spy_rename)
    second = _write(tmp_path, created_at_ms=1_900_000_000_000, force=True)
    assert seen == [[]]
    assert second.written is True
    assert read_manifest(second.path) == second.manifest
