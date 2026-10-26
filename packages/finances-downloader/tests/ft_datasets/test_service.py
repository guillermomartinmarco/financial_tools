"""T3.3 / T3.4 — Tarea ``materialize`` (fases RF-8, ``FakeRunner``) y ``lock`` (RF-7)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from finances_core.config import FinancesSettings
from finances_core.datasets import recipes
from finances_core.datasets.errors import InvalidRecipeError
from finances_core.schemas.datasets import MATERIALIZE_PHASE_ORDER, MaterializeResult
from structlog.testing import capture_logs

from finances_downloader.ft_datasets import materializer, service

from .conftest import (
    AAPL_KEY,
    ALL_KEYS,
    BTC_KEY,
    ETH_KEY,
    RECIPE_NAME,
    FakeRunner,
    write_item,
)

TASK_A = "0192f000-0000-7000-8000-00000000000a"
TASK_B = "0192f000-0000-7000-8000-00000000000b"


async def _materialize(
    settings: FinancesSettings,
    runner: FakeRunner,
    task_id: str = TASK_A,
    *,
    phases: list[str] | None = None,
    verify: bool = True,
    refetch: bool = False,
) -> MaterializeResult:
    return await service.run_materialize(
        RECIPE_NAME,
        task_id,
        settings=settings,
        runner=runner,
        verify=verify,
        refetch=refetch,
        on_phase=phases.append if phases is not None else None,
    )


def _lock_path(settings: FinancesSettings) -> Path:
    return recipes.lock_path(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)


# ------------------------------------------------------------------ T3.3 materialize
async def test_materialize_completes_unlocked_then_ok_after_lock(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    runner = FakeRunner()
    phases: list[str] = []
    result = await _materialize(settings, runner, phases=phases)
    assert result.status == "COMPLETED"
    assert result.outcome == "UNLOCKED"
    assert [b.exit_code for b in result.blocks] == [0, 0]
    assert phases == [*MATERIALIZE_PHASE_ORDER[1:], "COMPLETED"]
    assert not _lock_path(settings).exists()  # nunca se escribe el lock

    assert service.lock_recipe(RECIPE_NAME, settings=settings).outcome == "WRITTEN"
    report = service.verify(RECIPE_NAME, settings)
    assert report.status == "OK"
    assert [i.key for i in report.items] == ALL_KEYS


async def test_second_run_does_not_call_runner(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    await _materialize(settings, FakeRunner())
    service.lock_recipe(RECIPE_NAME, settings=settings)
    lock_bytes = _lock_path(settings).read_bytes()

    runner = FakeRunner()
    result = await _materialize(settings, runner, TASK_B)
    assert runner.calls == []
    assert result.status == "COMPLETED"
    assert result.outcome == "OK"
    assert result.blocks == []
    plan = service.get_plan(TASK_B, settings=settings)
    assert plan is not None
    assert plan.blocks == []
    assert plan.skipped_items == ALL_KEYS
    assert _lock_path(settings).read_bytes() == lock_bytes


async def test_runner_receives_absolute_datasets_dir_and_timeout(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    runner = FakeRunner()
    await _materialize(settings, runner)
    for _block, argv, env, timeout in runner.calls:
        assert Path(env["FT_PARQUET_DIR"]).is_absolute()
        assert Path(env["FT_PARQUET_DIR"]) == settings.DATASETS_DIR.resolve()
        assert env["FT_PARQUET_DIR"] != str(settings.PARQUET_DIR.resolve())
        assert timeout == settings.DATASETS_DOWNLOAD_TIMEOUT_S
        assert argv[1:5] == ["--log-format", "json", "-q", "download"]
    assert not settings.PARQUET_DIR.exists()


async def test_nonzero_exit_fails_and_keeps_previous_data(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    recipe = recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    btc = next(i for i in recipe.items() if i.key == BTC_KEY)
    write_item(settings.DATASETS_DIR, btc)
    before = {p: p.read_bytes() for p in settings.DATASETS_DIR.rglob("*.parquet")}

    runner = FakeRunner(exit_code=3, stderr=["boom 1", "boom 2"])
    phases: list[str] = []
    result = await _materialize(settings, runner, phases=phases)
    assert result.status == "FAILED"
    assert result.outcome == "DOWNLOAD_FAILED"
    assert len(result.blocks) == 1  # bloques en secuencia: se detiene en el primero
    assert result.blocks[0].stderr_tail == ["boom 1", "boom 2"]
    assert "exit 3" in result.errors[0]
    assert phases[-1] == "FAILED"
    assert {p: p.read_bytes() for p in settings.DATASETS_DIR.rglob("*.parquet")} == before
    stored = service.get_result(TASK_A, settings=settings)
    assert stored == result


async def test_timeout_fails_the_task(settings: FinancesSettings, recipe_dir: Path) -> None:
    result = await _materialize(settings, FakeRunner(timed_out=True))
    assert result.status == "FAILED"
    assert "timeout" in result.errors[0]


async def test_symbol_without_data_completes_with_missing_item(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    result = await _materialize(settings, FakeRunner(no_data_symbols={"ETHUSDT"}))
    assert result.status == "COMPLETED"
    assert result.outcome == "MISMATCH"
    assert result.verify is not None
    statuses = {i.key: i.status for i in result.verify.items}
    assert statuses[ETH_KEY] == "MISSING"
    assert statuses[BTC_KEY] == "UNLOCKED"


async def test_no_verify_outcome_and_progress(settings: FinancesSettings, recipe_dir: Path) -> None:
    result = await _materialize(settings, FakeRunner(), verify=False)
    assert result.outcome == "NOT_VERIFIED"
    assert result.verify is None
    progress = service.get_progress(TASK_A, settings=settings)
    assert progress is not None
    assert progress.status == "COMPLETED"
    assert progress.recipe == RECIPE_NAME
    assert (progress.items_total, progress.items_skipped) == (3, 0)
    assert (progress.blocks_total, progress.blocks_done) == (2, 2)
    assert service.get_result(TASK_A, settings=settings) == result


async def test_refetch_redownloads_mismatched_item(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    await _materialize(settings, FakeRunner())
    service.lock_recipe(RECIPE_NAME, settings=settings)
    recipe = recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    aapl = next(i for i in recipe.items() if i.key == AAPL_KEY)
    write_item(settings.DATASETS_DIR, aapl, scale=0.25)

    runner = FakeRunner()
    result = await _materialize(settings, runner, TASK_B, refetch=True)
    assert [call[1][0] for call in runner.calls] == ["ft_market_data_yfinance"]
    assert result.outcome == "OK"
    plan = service.get_plan(TASK_B, settings=settings)
    assert plan is not None
    assert plan.refetch_items == [AAPL_KEY]


async def test_invalid_recipe_in_task_marks_failed(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    (recipe_dir / f"{RECIPE_NAME}.toml").write_text("schema_version = 2\n", encoding="utf-8")
    result = await _materialize(settings, FakeRunner())
    assert result.status == "FAILED"
    assert "InvalidRecipeError" in result.errors[0]


def test_get_result_and_progress_of_unknown_task(settings: FinancesSettings) -> None:
    assert service.get_progress(TASK_A, settings=settings) is None
    assert service.get_result(TASK_A, settings=settings) is None


def test_get_result_of_running_task_raises(settings: FinancesSettings) -> None:
    service.create_materialize_task(TASK_A, RECIPE_NAME, settings=settings)
    with pytest.raises(service.TaskNotFinishedError):
        service.get_result(TASK_A, settings=settings)
    progress = service.get_progress(TASK_A, settings=settings)
    assert progress is not None
    assert progress.status == "PENDING"


def test_prepare_rejects_unsettled_range(settings: FinancesSettings, recipe_dir: Path) -> None:
    with pytest.raises(InvalidRecipeError, match="no reproducible"):
        service.prepare(RECIPE_NAME, settings, today=date(2024, 3, 3))
    service.prepare(RECIPE_NAME, settings, today=date(2024, 3, 8))


def test_dry_run_plan_does_not_register_task(settings: FinancesSettings, recipe_dir: Path) -> None:
    plan = service.dry_run_plan(RECIPE_NAME, settings=settings)
    assert len(plan.blocks) == 2
    assert service.get_progress(plan.task_id, settings=settings) is None
    assert materializer.load_materialize_plan(plan.task_id, settings) is None


# ------------------------------------------------------------------ T3.4 lock
def _fill(settings: FinancesSettings, *, skip: str | None = None) -> None:
    recipe = recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    for item in recipe.items():
        if item.key != skip:
            write_item(settings.DATASETS_DIR, item)


def test_lock_written_then_unchanged_without_rewrite(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    _fill(settings)
    first = service.lock_recipe(RECIPE_NAME, settings=settings)
    assert first.outcome == "WRITTEN"
    assert first.differences == []
    path = _lock_path(settings)
    assert first.lock_path == str(path)
    lock = recipes.load_lock(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    assert lock is not None
    assert [i.key for i in lock.items] == ALL_KEYS
    assert set(lock.tool_versions) == {"finances-downloader", "polars", "yfinance"}
    mtime, content = path.stat().st_mtime_ns, path.read_bytes()

    again = service.lock_recipe(RECIPE_NAME, settings=settings)
    assert again.outcome == "UNCHANGED"
    assert path.stat().st_mtime_ns == mtime
    assert path.read_bytes() == content


def test_lock_rejected_when_data_changed_and_updated_with_flag(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    _fill(settings)
    service.lock_recipe(RECIPE_NAME, settings=settings)
    path = _lock_path(settings)
    original = path.read_bytes()
    recipe = recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    aapl = next(i for i in recipe.items() if i.key == AAPL_KEY)
    write_item(settings.DATASETS_DIR, aapl, scale=0.25)

    rejected = service.lock_recipe(RECIPE_NAME, settings=settings)
    assert rejected.outcome == "REJECTED"
    assert len(rejected.differences) == 1
    assert rejected.differences[0].startswith(f"{AAPL_KEY}: ")
    assert "content_hash" in rejected.differences[0]
    assert path.read_bytes() == original

    with capture_logs() as logs:
        updated = service.lock_recipe(RECIPE_NAME, update=True, settings=settings)
    assert updated.outcome == "UPDATED"
    assert updated.differences == rejected.differences
    assert path.read_bytes() != original
    warning = next(e for e in logs if e["event"] == "dataset_lock_updated")
    assert warning["log_level"] == "warning"
    assert warning["differences"] == updated.differences
    assert service.verify(RECIPE_NAME, settings).status == "OK"


def test_lock_rejected_when_recipe_changed(settings: FinancesSettings, recipe_dir: Path) -> None:
    _fill(settings)
    service.lock_recipe(RECIPE_NAME, settings=settings)
    toml = recipe_dir / f"{RECIPE_NAME}.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace('["BTCUSDT", "ETHUSDT"]', '["BTCUSDT"]'),
        encoding="utf-8",
    )
    result = service.lock_recipe(RECIPE_NAME, settings=settings)
    assert result.outcome == "REJECTED"
    assert result.differences[0].startswith("recipe_sha256:")
    assert any("eliminado" in d and ETH_KEY in d for d in result.differences)
    assert service.verify(RECIPE_NAME, settings).status == "RECIPE_CHANGED"


def test_lock_with_missing_item_writes_nothing(
    settings: FinancesSettings, recipe_dir: Path
) -> None:
    _fill(settings, skip=ETH_KEY)
    result = service.lock_recipe(RECIPE_NAME, settings=settings)
    assert result.outcome == "REJECTED"
    assert result.differences == [
        f"{ETH_KEY}: MISSING (sin filas; nunca se bloquea un dataset vacío)"
    ]
    assert not _lock_path(settings).exists()
    leftovers = list(recipe_dir.glob("*.tmp"))
    assert leftovers == []


def test_list_and_show(settings: FinancesSettings, recipe_dir: Path) -> None:
    (recipe_dir / "broken.toml").write_text("schema_version = 1\n", encoding="utf-8")
    summaries = {s.name: s for s in service.list_recipes(settings)}
    assert summaries[RECIPE_NAME].lock == "UNLOCKED"
    assert summaries[RECIPE_NAME].items == 3
    assert summaries["broken"].lock == "INVALID_RECIPE"
    _fill(settings)
    service.lock_recipe(RECIPE_NAME, settings=settings)
    assert {s.name: s for s in service.list_recipes(settings)}[RECIPE_NAME].lock == "LOCKED"
    detail = service.show_recipe(RECIPE_NAME, settings)
    assert [i.key for i in detail.items] == ALL_KEYS
    assert detail.lock is not None
