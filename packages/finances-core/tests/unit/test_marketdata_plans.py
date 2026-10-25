"""T1.3: persistencia de planes en ``share/plans`` y validación de rango."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel

from finances_core.config import FinancesSettings
from finances_core.marketdata.plans import (
    FUTURE_DATA_MESSAGE,
    load_plan,
    new_task_id,
    plan_file_path,
    save_plan,
    validate_time_range,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
NOW = 1_700_000_000_000


class _Plan(BaseModel):
    task_id: str
    items: list[int]


def test_new_task_id_is_uuid7() -> None:
    task_id = new_task_id()
    assert uuid.UUID(task_id).version == 7
    assert len(task_id) == 36


def test_plan_file_path_under_share_plans(tmp_path: Path) -> None:
    path = plan_file_path("abc")
    assert path == tmp_path.resolve() / "share" / "plans" / "download_plan_abc.json"


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    plan = _Plan(task_id="t-1", items=[1, 2])
    path = save_plan("t-1", plan)
    assert path == tmp_path.resolve() / "share" / "plans" / "download_plan_t-1.json"
    assert load_plan("t-1", _Plan) == plan


def test_load_missing_plan_returns_none() -> None:
    assert load_plan("nope", _Plan) is None


def test_save_plan_with_explicit_settings(tmp_path: Path) -> None:
    settings = FinancesSettings(SHARE_DIR=tmp_path / "other")
    save_plan("t-2", _Plan(task_id="t-2", items=[]), settings=settings)
    assert (tmp_path / "other" / "plans" / "download_plan_t-2.json").is_file()
    assert load_plan("t-2", _Plan, settings=settings) is not None
    assert load_plan("t-2", _Plan) is None


def test_validate_time_range_clamps_future_end() -> None:
    assert validate_time_range(NOW - DAY_MS, NOW + DAY_MS, {"1h": HOUR_MS}, NOW) == NOW
    assert validate_time_range(NOW - DAY_MS, None, {"1h": HOUR_MS}, NOW) == NOW
    assert validate_time_range(NOW - DAY_MS, NOW - HOUR_MS, {"1h": HOUR_MS}, NOW) == NOW - HOUR_MS


def test_validate_time_range_rejects_future_start_for_any_timeframe() -> None:
    with pytest.raises(ValueError, match=FUTURE_DATA_MESSAGE):
        validate_time_range(NOW - HOUR_MS, None, {"1h": HOUR_MS, "1d": DAY_MS}, NOW)


def test_validate_time_range_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="end_ms"):
        validate_time_range(NOW - DAY_MS, NOW - 2 * DAY_MS, {"1h": HOUR_MS}, NOW)


# --- T1.2 (ft_indicators / ft_ta_lib): prefijo de plan por herramienta ----------------


@pytest.mark.parametrize("prefix", ["indicators_plan", "ta_lib_plan"])
def test_plan_prefix_roundtrip(tmp_path: Path, prefix: str) -> None:
    plan = _Plan(task_id="t-9", items=[3])
    path = save_plan("t-9", plan, prefix=prefix)
    assert path == tmp_path.resolve() / "share" / "plans" / f"{prefix}_t-9.json"
    assert plan_file_path("t-9", prefix=prefix) == path
    assert load_plan("t-9", _Plan, prefix=prefix) == plan
    # El prefijo por defecto sigue siendo el de los descargadores.
    assert load_plan("t-9", _Plan) is None


def test_plan_prefix_rejects_unsafe_names() -> None:
    with pytest.raises(ValueError, match="prefix"):
        plan_file_path("t", prefix="../evil")
