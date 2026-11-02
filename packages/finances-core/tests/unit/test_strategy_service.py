"""Servicio de ``ft_strategy_manager`` (T2.1-T2.7, T3.2, T6.2).

Usa una base SQLite temporal real (aislada por ``FT_PROJECT_ROOT`` del conftest): las
reglas de negocio dependen de las restricciones de la base, así que no se simula.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml
from structlog.testing import capture_logs

from finances_core.schemas.strategy import (
    StrategyCreate,
    StrategyListFilters,
    StrategyUpdate,
)
from ft_strategy_manager import db, service
from ft_strategy_manager.errors import (
    NoChangesError,
    StrategyConflictError,
    StrategyNotFoundError,
    StrategyValidationError,
    VersionNotFoundError,
)
from ft_strategy_manager.utils import MASK, compute_hash

UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"window": {"type": "integer", "minimum": 1}},
    "required": ["window"],
}


@pytest.fixture(autouse=True)
def _default_structlog() -> None:
    # Otros tests configuran un nivel filtrante (p. ej. --quiet) que ocultaría el INFO de auditoría.
    structlog.reset_defaults()


def _create(name: str = "alpha", **overrides: Any) -> str:
    fields: dict[str, Any] = {"name": name, "parameters": {"window": 10, "mode": "a"}}
    fields.update(overrides)
    return service.create_strategy(StrategyCreate(**fields)).strategy_id


# --------------------------------------------------------------------------- create
def test_create_returns_v1_with_hash_and_persists() -> None:
    params = {"window": 10, "nested": {"b": [1, 2]}}
    created = service.create_strategy(
        StrategyCreate(name="alpha", parameters=params, tags=["fx"]), user="ana"
    )
    assert UUID7_RE.match(created.strategy_id)
    assert created.version == 1
    assert created.hash == compute_hash(params)
    assert created.created_by == "ana"
    assert created.created_at_ms > 1_600_000_000_000

    stored = service.get_strategy(created.strategy_id)
    assert stored.name == "alpha"
    assert stored.tags == ["fx"]
    assert stored.versions == [created]


def test_create_duplicate_name_conflicts_and_writes_nothing() -> None:
    _create("alpha")
    with pytest.raises(StrategyConflictError):
        _create("alpha")
    assert service.list_strategies(StrategyListFilters()).total_count == 1


def test_create_soft_deleted_name_still_conflicts() -> None:
    strategy_id = _create("alpha")
    service.delete_strategy(strategy_id)
    with pytest.raises(StrategyConflictError):
        _create("alpha")


def test_create_validates_against_schema_with_detailed_errors() -> None:
    with pytest.raises(StrategyValidationError, match="window"):
        service.create_strategy(
            StrategyCreate(name="alpha", parameters={"window": 0}, parameter_schema=SCHEMA)
        )
    with pytest.raises(StrategyValidationError, match="required"):
        service.create_strategy(
            StrategyCreate(name="alpha", parameters={}, parameter_schema=SCHEMA)
        )
    assert service.list_strategies(StrategyListFilters()).total_count == 0


def test_create_rejects_invalid_json_schema() -> None:
    with pytest.raises(StrategyValidationError, match="Invalid parameter_schema"):
        service.create_strategy(
            StrategyCreate(name="alpha", parameters={}, parameter_schema={"type": "bogus"})
        )


def test_create_without_schema_is_permissive() -> None:
    strategy_id = _create("alpha", parameters={"anything": [1, {"x": None}]})
    assert service.get_strategy(strategy_id).versions[0].parameters == {
        "anything": [1, {"x": None}]
    }


def test_create_logs_audit_with_masked_secrets() -> None:
    with capture_logs() as logs:
        created = service.create_strategy(
            StrategyCreate(name="alpha", parameters={"api_key": "s3cr3t", "w": 1})
        )
    audit = next(e for e in logs if e["event"] == "strategy_audit")
    assert audit["log_level"] == "info"
    assert audit["action"] == "create"
    assert audit["strategy_id"] == created.strategy_id
    assert audit["version"] == 1
    assert audit["hash_before"] is None
    assert audit["hash_after"] == created.hash
    assert audit["diff_summary"] == {}
    assert audit["user"] is None
    assert "s3cr3t" not in json.dumps(logs, default=str)


# ------------------------------------------------------------------------------- get
def test_get_returns_all_versions_and_latest_by_default() -> None:
    strategy_id = _create("alpha")
    service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 20}))
    strategy = service.get_strategy(strategy_id)
    assert [v.version for v in strategy.versions] == [1, 2]
    assert strategy.selected_version == 2
    assert strategy.selected is not None
    assert strategy.selected.parameters == {"window": 20}


def test_get_specific_version() -> None:
    strategy_id = _create("alpha")
    service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 20}))
    strategy = service.get_strategy(strategy_id, 1)
    assert strategy.selected_version == 1
    assert strategy.selected is not None
    assert strategy.selected.parameters == {"window": 10, "mode": "a"}
    assert len(strategy.versions) == 2


def test_get_missing_strategy_and_version() -> None:
    strategy_id = _create("alpha")
    with pytest.raises(StrategyNotFoundError, match="Strategy not found"):
        service.get_strategy("00000000-0000-7000-8000-000000000000")
    with pytest.raises(VersionNotFoundError):
        service.get_strategy(strategy_id, 7)


def test_get_returns_soft_deleted_strategy_for_audit() -> None:
    strategy_id = _create("alpha")
    service.delete_strategy(strategy_id)
    assert service.get_strategy(strategy_id).deleted_at_ms is not None


# ---------------------------------------------------------------------------- update
def test_update_full_replace_creates_incremental_versions_and_keeps_old_intact() -> None:
    strategy_id = _create("alpha")
    v1_before = service.get_strategy(strategy_id).versions[0]
    v2 = service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 20}), user="bo")
    v3 = service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 30}))
    assert (v2.version, v3.version) == (2, 3)
    assert v2.hash == compute_hash({"window": 20})
    assert v2.hash != v1_before.hash
    assert v2.created_by == "bo"
    versions = service.get_strategy(strategy_id).versions
    assert versions[0] == v1_before  # inmutabilidad histórica
    assert [v.version for v in versions] == [1, 2, 3]


def test_update_with_json_patch_applies_to_latest() -> None:
    strategy_id = _create("alpha", parameters={"window": 10, "mode": "a", "extra": 1})
    version = service.update_strategy(
        strategy_id,
        StrategyUpdate(
            parameters_patch=[
                {"op": "replace", "path": "/window", "value": 99},
                {"op": "remove", "path": "/extra"},
            ]
        ),
    )
    assert version.parameters == {"window": 99, "mode": "a"}


def test_update_rejects_invalid_patch_without_creating_version() -> None:
    strategy_id = _create("alpha")
    with pytest.raises(StrategyValidationError, match="Invalid parameters_patch"):
        service.update_strategy(
            strategy_id, StrategyUpdate(parameters_patch=[{"op": "remove", "path": "/nope"}])
        )
    assert len(service.get_strategy(strategy_id).versions) == 1


def test_update_validates_against_stored_schema() -> None:
    strategy_id = service.create_strategy(
        StrategyCreate(name="alpha", parameters={"window": 5}, parameter_schema=SCHEMA)
    ).strategy_id
    with pytest.raises(StrategyValidationError, match="window"):
        service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": -1}))
    assert len(service.get_strategy(strategy_id).versions) == 1


def test_update_without_changes_is_rejected() -> None:
    strategy_id = _create("alpha", parameters={"a": 1, "b": 2})
    with pytest.raises(NoChangesError, match="No changes detected"):
        service.update_strategy(strategy_id, StrategyUpdate(parameters={"b": 2, "a": 1}))
    assert len(service.get_strategy(strategy_id).versions) == 1


def test_update_allows_empty_parameters_replacement() -> None:
    strategy_id = _create("alpha")
    assert service.update_strategy(strategy_id, StrategyUpdate(parameters={})).parameters == {}


def test_update_changes_description_and_tags() -> None:
    strategy_id = _create("alpha", tags=["old"])
    service.update_strategy(
        strategy_id, StrategyUpdate(parameters={"window": 2}, description="new", tags=[])
    )
    strategy = service.get_strategy(strategy_id)
    assert strategy.description == "new"
    assert strategy.tags == []


def test_update_missing_or_soft_deleted_strategy_is_not_found() -> None:
    with pytest.raises(StrategyNotFoundError):
        service.update_strategy("ghost", StrategyUpdate(parameters={"a": 1}))
    strategy_id = _create("alpha")
    service.delete_strategy(strategy_id)
    with pytest.raises(StrategyNotFoundError):
        service.update_strategy(strategy_id, StrategyUpdate(parameters={"a": 1}))


def test_update_logs_audit_with_diff_summary_and_masks_secrets() -> None:
    strategy_id = _create("alpha", parameters={"window": 1, "password": "old-pw"})
    with capture_logs() as logs:
        service.update_strategy(
            strategy_id,
            StrategyUpdate(parameters={"window": 2, "password": "new-pw"}),
            user="ana",
        )
    audit = next(e for e in logs if e["event"] == "strategy_audit")
    assert audit["action"] == "update"
    assert audit["version"] == 2
    assert audit["user"] == "ana"
    assert audit["hash_before"] != audit["hash_after"]
    assert audit["diff_summary"]["changed"] == {"window": [1, 2], "password": MASK}
    assert "old-pw" not in json.dumps(logs, default=str)
    assert "new-pw" not in json.dumps(logs, default=str)


# ---------------------------------------------------------------------------- delete
def test_soft_delete_hides_from_listing_but_keeps_versions() -> None:
    strategy_id = _create("alpha")
    service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 2}))
    service.delete_strategy(strategy_id)
    assert service.list_strategies(StrategyListFilters()).total_count == 0
    included = service.list_strategies(StrategyListFilters(include_deleted=True))
    assert included.total_count == 1
    assert included.items[0].deleted_at_ms is not None
    assert len(service.get_strategy(strategy_id).versions) == 2


def test_soft_delete_twice_keeps_first_timestamp() -> None:
    strategy_id = _create("alpha")
    service.delete_strategy(strategy_id)
    first = service.get_strategy(strategy_id).deleted_at_ms
    with capture_logs() as logs:
        service.delete_strategy(strategy_id)
    assert service.get_strategy(strategy_id).deleted_at_ms == first
    assert not [e for e in logs if e["event"] == "strategy_audit"]


def test_purge_removes_rows_in_cascade_and_is_idempotent() -> None:
    strategy_id = _create("alpha")
    service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 2}))
    service.delete_strategy(strategy_id, purge=True)
    with pytest.raises(StrategyNotFoundError):
        service.get_strategy(strategy_id)
    with db.get_connection(db.resolve_db_path()) as conn:
        assert db.list_versions(conn, strategy_id) == []
    service.delete_strategy(strategy_id, purge=True)
    service.delete_strategy("never-existed")
    assert _create("alpha")  # el nombre queda libre tras el purge


def test_purge_after_soft_delete() -> None:
    strategy_id = _create("alpha")
    service.delete_strategy(strategy_id)
    service.delete_strategy(strategy_id, purge=True)
    with pytest.raises(StrategyNotFoundError):
        service.get_strategy(strategy_id)


def test_delete_logs_audit_for_soft_and_purge() -> None:
    strategy_id = _create("alpha")
    with capture_logs() as logs:
        service.delete_strategy(strategy_id, user="ana")
        service.delete_strategy(strategy_id, purge=True, user="ana")
    audits = [e for e in logs if e["event"] == "strategy_audit"]
    assert [a["action"] for a in audits] == ["delete", "delete"]
    assert [a["diff_summary"] for a in audits] == [{"purge": False}, {"purge": True}]
    assert all(a["hash_before"] == a["hash_after"] is not None for a in audits)
    assert all(a["strategy_id"] == strategy_id and a["user"] == "ana" for a in audits)


# ------------------------------------------------------------------------------ list
def test_list_pagination_filters_and_summary_fields() -> None:
    first = _create("trend_follow", tags=["trend", "fx"])
    _create("mean_rev", tags=["fx"])
    third = _create("pairs")
    service.update_strategy(first, StrategyUpdate(parameters={"window": 77}))

    everything = service.list_strategies(StrategyListFilters())
    assert everything.total_count == 3
    assert (everything.limit, everything.offset) == (50, 0)

    page = service.list_strategies(StrategyListFilters(limit=2, offset=2))
    assert page.total_count == 3
    assert len(page.items) == 1

    assert {s.name for s in service.list_strategies(StrategyListFilters(tag="fx")).items} == {
        "trend_follow",
        "mean_rev",
    }
    by_name = service.list_strategies(StrategyListFilters(name_contains="REV"))
    assert [s.name for s in by_name.items] == ["mean_rev"]

    summary = next(s for s in everything.items if s.id == first)
    assert summary.latest_version == 2
    assert summary.hash_latest == compute_hash({"window": 77})
    assert summary.tags == ["trend", "fx"]
    assert summary.updated_at_ms >= service.get_strategy(first).created_at_ms
    assert next(s for s in everything.items if s.id == third).latest_version == 1


# ------------------------------------------------------------------------------ diff
def test_diff_reports_added_removed_changed_with_json_types() -> None:
    strategy_id = _create("alpha", parameters={"a": 1, "b": [1], "gone": "x", "flag": True})
    service.update_strategy(
        strategy_id,
        StrategyUpdate(parameters={"a": 1.0, "b": [1, 2], "new": {"k": None}, "flag": True}),
    )
    diff = service.diff_versions(strategy_id, 1, 2)
    assert (diff.from_version, diff.to_version) == (1, 2)
    assert diff.added == {"new": {"k": None}}
    assert diff.removed == {"gone": "x"}
    assert diff.changed == {"a": (1, 1.0), "b": ([1], [1, 2])}


def test_diff_order_of_versions_is_irrelevant_for_validity() -> None:
    strategy_id = _create("alpha", parameters={"a": 1})
    service.update_strategy(strategy_id, StrategyUpdate(parameters={"a": 2}))
    forward = service.diff_versions(strategy_id, 1, 2)
    backward = service.diff_versions(strategy_id, 2, 1)
    assert forward.changed == {"a": (1, 2)}
    assert backward.changed == {"a": (2, 1)}
    same = service.diff_versions(strategy_id, 2, 2)
    assert (same.added, same.removed, same.changed) == ({}, {}, {})


def test_diff_missing_strategy_or_version() -> None:
    strategy_id = _create("alpha")
    with pytest.raises(StrategyNotFoundError):
        service.diff_versions("ghost", 1, 2)
    with pytest.raises(VersionNotFoundError) as info:
        service.diff_versions(strategy_id, 1, 5)
    assert info.value.version == 5


# ---------------------------------------------------------------------------- export
def test_export_json_contains_strategy_versions_and_metadata() -> None:
    strategy_id = _create("alpha", description="d", tags=["fx"])
    service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 2}))
    document = json.loads(service.export_strategy(strategy_id, "json"))
    assert document["strategy"]["id"] == strategy_id
    assert document["strategy"]["name"] == "alpha"
    assert document["strategy"]["description"] == "d"
    assert document["strategy"]["tags"] == ["fx"]
    assert [v["version"] for v in document["strategy"]["versions"]] == [1, 2]
    assert document["exported_at_ms"] > 0


def test_export_yaml_roundtrips_same_document() -> None:
    strategy_id = _create("alpha", parameters={"unicode": "café", "n": [1, 2.5, True, None]})
    as_json = json.loads(service.export_strategy(strategy_id, "json"))
    as_yaml = yaml.safe_load(service.export_strategy(strategy_id, "yaml"))
    as_yaml["exported_at_ms"] = as_json["exported_at_ms"]
    assert as_yaml == as_json


def test_export_missing_strategy_and_unknown_format() -> None:
    with pytest.raises(StrategyNotFoundError):
        service.export_strategy("ghost", "json")
    strategy_id = _create("alpha")
    with pytest.raises(StrategyValidationError, match="Unsupported export format"):
        service.export_strategy(strategy_id, "xml")  # type: ignore[arg-type]


# -------------------------------------------------------------------------- versions
def test_list_versions_paginates_in_ascending_order() -> None:
    strategy_id = _create("alpha")
    for window in (2, 3, 4):
        service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": window}))
    assert [v.version for v in service.list_versions(strategy_id)] == [1, 2, 3, 4]
    assert [v.version for v in service.list_versions(strategy_id, limit=2, offset=1)] == [2, 3]
    with pytest.raises(StrategyNotFoundError):
        service.list_versions("ghost")


# ------------------------------------------------------------------------ db location
def test_service_uses_env_overridden_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere" / "custom.db"
    monkeypatch.setenv(db.DB_PATH_ENV, str(target))
    _create("alpha")
    assert target.is_file()
    assert not (tmp_path / "share" / "strategies.db").exists()


def test_service_recreates_database_deleted_between_calls(tmp_path: Path) -> None:
    _create("alpha")
    path = db.resolve_db_path()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    assert service.list_strategies(StrategyListFilters()).total_count == 0


# ------------------------------------------------- Deriva 10: snapshot de metadatos
def test_create_snapshots_metadata_in_v1() -> None:
    created = service.create_strategy(
        StrategyCreate(name="alpha", parameters={"a": 1}, description="d1", tags=["x"])
    )
    assert (created.description, created.tags) == ("d1", ["x"])
    assert service.get_strategy(created.strategy_id).versions == [created]


def test_update_metadata_only_creates_new_version_keeping_parameters() -> None:
    strategy_id = _create("alpha", description="old", tags=["a"])
    v1 = service.get_strategy(strategy_id).versions[0]
    v2 = service.update_strategy(strategy_id, StrategyUpdate(description="new"), user="ana")
    assert v2.version == 2
    assert v2.parameters == v1.parameters
    assert v2.hash == v1.hash  # el hash es solo de parámetros (RF-1)
    assert (v2.description, v2.tags) == ("new", ["a"])  # tags heredados
    versions = service.get_strategy(strategy_id).versions
    assert versions[0] == v1  # v1 conserva su snapshot original
    assert versions[0].description == "old"


def test_update_tags_only_creates_new_version() -> None:
    strategy_id = _create("alpha", description="d", tags=["a"])
    v2 = service.update_strategy(strategy_id, StrategyUpdate(tags=["b", "c"]))
    assert (v2.version, v2.description, v2.tags) == (2, "d", ["b", "c"])
    strategy = service.get_strategy(strategy_id)
    assert (strategy.description, strategy.tags) == ("d", ["b", "c"])


def test_update_inherits_metadata_when_only_parameters_change() -> None:
    strategy_id = _create("alpha", description="d", tags=["a"])
    v2 = service.update_strategy(strategy_id, StrategyUpdate(parameters={"window": 2}))
    assert (v2.description, v2.tags) == ("d", ["a"])


def test_update_with_same_parameters_and_same_metadata_is_rejected() -> None:
    strategy_id = _create("alpha", description="d", tags=["a"])
    with pytest.raises(NoChangesError, match="No changes detected"):
        service.update_strategy(strategy_id, StrategyUpdate(description="d", tags=["a"]))
    with pytest.raises(NoChangesError, match="No changes detected"):
        service.update_strategy(
            strategy_id, StrategyUpdate(parameters={"window": 10, "mode": "a"}, tags=["a"])
        )
    assert len(service.get_strategy(strategy_id).versions) == 1


def test_update_metadata_change_logs_audit_with_same_hash() -> None:
    strategy_id = _create("alpha", description="old")
    with capture_logs() as logs:
        service.update_strategy(strategy_id, StrategyUpdate(description="new"))
    event = next(e for e in logs if e["event"] == "strategy_audit" and e["action"] == "update")
    assert event["hash_before"] == event["hash_after"]
    assert event["diff_summary"]["metadata_changed"] == {
        "description": ["old", "new"]  # mask_secrets convierte tuplas en listas
    }


def test_diff_reports_metadata_changes() -> None:
    strategy_id = _create("alpha", description="old", tags=["a"])
    service.update_strategy(strategy_id, StrategyUpdate(description="new", tags=["a", "b"]))
    result = service.diff_versions(strategy_id, 1, 2)
    assert (result.added, result.removed, result.changed) == ({}, {}, {})
    assert result.metadata_changed == {
        "description": ("old", "new"),
        "tags": (["a"], ["a", "b"]),
    }
    assert service.diff_versions(strategy_id, 1, 1).metadata_changed == {}


def test_export_includes_version_snapshots() -> None:
    strategy_id = _create("alpha", description="d", tags=["a"])
    service.update_strategy(strategy_id, StrategyUpdate(tags=["b"]))
    document = json.loads(service.export_strategy(strategy_id))
    assert [v["tags"] for v in document["strategy"]["versions"]] == [["a"], ["b"]]
