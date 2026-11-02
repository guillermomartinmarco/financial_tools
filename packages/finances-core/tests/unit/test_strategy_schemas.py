"""Contratos Pydantic de ``ft_strategy_manager`` (T0.1)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from finances_core.schemas.strategy import (
    DiffResult,
    Strategy,
    StrategyCreate,
    StrategyListFilters,
    StrategyListResponse,
    StrategySummary,
    StrategyUpdate,
    StrategyVersion,
)

HASH = "a" * 64


def test_create_accepts_valid_example() -> None:
    payload = StrategyCreate.model_validate(
        {
            "name": "mean_reversion-v1",
            "description": "demo",
            "parameters": {"window": 20, "nested": {"flags": [True, 1.5, "x", None]}},
            "tags": ["fx"],
        }
    )
    assert payload.parameters["window"] == 20
    assert payload.parameter_schema is None


@pytest.mark.parametrize("name", ["ab", "Upper", "1abc", "has space", "a" * 65, ""])
def test_create_rejects_invalid_name(name: str) -> None:
    with pytest.raises(ValidationError):
        StrategyCreate(name=name)


def test_create_rejects_long_description() -> None:
    with pytest.raises(ValidationError):
        StrategyCreate(name="abc", description="x" * 501)


@pytest.mark.parametrize(
    "parameters",
    [{"a": {1, 2}}, {"a": b"raw"}, {"a": float("nan")}, {"a": {"b": float("inf")}}, {"a": (1,)}],
)
def test_create_rejects_non_json_parameters(parameters: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StrategyCreate(name="abc", parameters=parameters)


def test_create_rejects_non_string_keys() -> None:
    with pytest.raises(ValidationError):
        StrategyCreate(name="abc", parameters={"a": {1: "x"}})


def test_create_rejects_non_json_schema_values() -> None:
    with pytest.raises(ValidationError):
        StrategyCreate(name="abc", parameter_schema={"x": {1, 2}})


def test_update_requires_exactly_one_of_parameters_or_patch() -> None:
    with pytest.raises(ValidationError, match="not both"):
        StrategyUpdate(parameters={"a": 1}, parameters_patch=[])
    with pytest.raises(ValidationError, match="Must provide"):
        StrategyUpdate()
    assert StrategyUpdate(parameters={}).parameters == {}
    assert StrategyUpdate(parameters_patch=[{"op": "remove", "path": "/a"}]).parameters is None


def test_update_rejects_non_json_parameters() -> None:
    with pytest.raises(ValidationError):
        StrategyUpdate(parameters={"a": object()})


def test_version_hash_must_be_sha256_hex() -> None:
    base: dict[str, Any] = {
        "strategy_id": "id",
        "version": 1,
        "parameters": {},
        "created_at_ms": 1,
    }
    assert StrategyVersion(hash=HASH, **base).created_by is None
    with pytest.raises(ValidationError):
        StrategyVersion(hash="XYZ", **base)
    with pytest.raises(ValidationError):
        StrategyVersion(hash=HASH, **{**base, "version": 0})


def _version(number: int) -> StrategyVersion:
    return StrategyVersion(
        strategy_id="id", version=number, parameters={"v": number}, hash=HASH, created_at_ms=number
    )


def test_strategy_latest_and_selected_version() -> None:
    strategy = Strategy(
        id="id",
        name="abc",
        description=None,
        parameter_schema=None,
        tags=[],
        created_at_ms=1,
        versions=[_version(1), _version(3), _version(2)],
    )
    assert strategy.latest_version is not None
    assert strategy.latest_version.version == 3
    assert strategy.selected == strategy.latest_version
    strategy.selected_version = 2
    assert strategy.selected is not None
    assert strategy.selected.version == 2
    strategy.selected_version = 9
    assert strategy.selected is None


def test_strategy_without_versions_has_no_latest() -> None:
    strategy = Strategy(
        id="id", name="abc", description=None, parameter_schema=None, tags=[], created_at_ms=1
    )
    assert strategy.latest_version is None


def test_filters_bounds() -> None:
    assert StrategyListFilters().limit == 50
    with pytest.raises(ValidationError):
        StrategyListFilters(limit=501)
    with pytest.raises(ValidationError):
        StrategyListFilters(limit=0)
    with pytest.raises(ValidationError):
        StrategyListFilters(offset=-1)


def test_summary_list_and_diff_roundtrip() -> None:
    summary = StrategySummary(
        id="id", name="abc", latest_version=2, updated_at_ms=5, tags=["t"], hash_latest=HASH
    )
    listing = StrategyListResponse(items=[summary], total_count=1, limit=50, offset=0)
    assert StrategyListResponse.model_validate_json(listing.model_dump_json()) == listing
    diff = DiffResult(from_version=1, to_version=2, added={}, removed={}, changed={"a": (1, 2)})
    assert diff.model_dump(mode="json")["changed"] == {"a": [1, 2]}
