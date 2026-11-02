"""Persistencia SQLite real de ``ft_strategy_manager`` (T1.1, T1.2, T6.3)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from finances_core.schemas.strategy import StrategyListFilters
from ft_strategy_manager import db
from ft_strategy_manager.errors import StrategyConflictError

HASH = "b" * 64


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "nested" / "strategies.db"
    db.init_db(path)
    return path


def _make_strategy(  # noqa: PLR0913
    path: Path,
    strategy_id: str,
    name: str,
    *,
    tags: list[str] | None = None,
    created_at_ms: int = 1,
    versions: int = 1,
) -> None:
    with db.get_connection(path, write=True) as conn:
        db.create_strategy_row(
            conn,
            strategy_id=strategy_id,
            name=name,
            description=None,
            parameter_schema=None,
            tags=tags or [],
            created_at_ms=created_at_ms,
        )
        for number in range(1, versions + 1):
            db.create_version_row(
                conn,
                strategy_id=strategy_id,
                version=number,
                parameters={"n": number},
                hash_=f"{number:064x}",
                created_at_ms=created_at_ms + number,
                created_by=None,
            )


def test_init_db_creates_schema_indexes_and_wal(db_path: Path) -> None:
    assert db_path.is_file()
    with db.get_connection(db_path) as conn:
        tables = {r["name"] for r in db.fetch_all(conn, "SELECT name FROM sqlite_master")}
        mode = db.fetch_one(conn, "PRAGMA journal_mode")
        columns = {r["name"] for r in db.fetch_all(conn, "PRAGMA table_info(strategies)")}
        version_columns = {
            r["name"] for r in db.fetch_all(conn, "PRAGMA table_info(strategy_versions)")
        }
    assert {"strategies", "strategy_versions", "idx_strategies_name"} <= tables
    assert {"idx_versions_strategy_id", "idx_versions_strategy_version"} <= tables
    assert mode is not None
    assert mode[0] == "wal"
    assert columns == {
        "id",
        "name",
        "description",
        "parameter_schema",
        "tags",
        "created_at_ms",
        "deleted_at_ms",
    }
    assert version_columns == {
        "id",
        "strategy_id",
        "version",
        "parameters_json",
        "hash",
        "created_at_ms",
        "created_by",
    }


def test_init_db_is_idempotent(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha")
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        assert db.get_strategy_row(conn, "s1") is not None


def test_concurrent_reader_not_blocked_by_open_writer(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha")
    seen: list[int] = []

    with db.get_connection(db_path, write=True) as writer:
        db.create_strategy_row(
            writer,
            strategy_id="s2",
            name="beta",
            description=None,
            parameter_schema=None,
            tags=[],
            created_at_ms=2,
        )

        def read() -> None:
            with db.get_connection(db_path) as reader:
                row = db.fetch_one(reader, "SELECT COUNT(*) AS n FROM strategies")
                assert row is not None
                seen.append(row["n"])

        thread = threading.Thread(target=read)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert seen == [1]  # el lector ve el último commit sin esperar al escritor


def test_write_transaction_rolls_back_on_error(db_path: Path) -> None:
    with pytest.raises(RuntimeError), db.get_connection(db_path, write=True) as conn:
        db.create_strategy_row(
            conn,
            strategy_id="s1",
            name="alpha",
            description=None,
            parameter_schema=None,
            tags=[],
            created_at_ms=1,
        )
        raise RuntimeError("boom")
    with db.get_connection(db_path) as conn:
        assert db.get_strategy_row(conn, "s1") is None


def test_execute_and_execute_many_helpers(db_path: Path) -> None:
    with db.get_connection(db_path, write=True) as conn:
        inserted = db.execute_many(
            conn,
            "INSERT INTO strategies (id, name, tags, created_at_ms) VALUES (?, ?, '[]', ?)",
            [("a", "aaa", 1), ("b", "bbb", 2)],
        )
        deleted = db.execute(conn, "DELETE FROM strategies WHERE id = ?", ("a",))
    assert (inserted, deleted) == (2, 1)


def test_roundtrip_strategy_row(db_path: Path) -> None:
    with db.get_connection(db_path, write=True) as conn:
        db.create_strategy_row(
            conn,
            strategy_id="s1",
            name="alpha",
            description="desc",
            parameter_schema={"type": "object"},
            tags=["x", "y"],
            created_at_ms=10,
        )
    with db.get_connection(db_path) as conn:
        strategy = db.get_strategy_row(conn, "s1")
        assert db.get_strategy_row(conn, "nope") is None
        assert db.name_exists(conn, "alpha")
        assert not db.name_exists(conn, "other")
    assert strategy is not None
    assert strategy.parameter_schema == {"type": "object"}
    assert strategy.tags == ["x", "y"]
    assert strategy.description == "desc"
    assert strategy.deleted_at_ms is None
    assert strategy.versions == []


def test_unique_name_raises_conflict(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha")
    with pytest.raises(StrategyConflictError) as info:
        _make_strategy(db_path, "s2", "alpha")
    assert info.value.name == "alpha"


def test_unique_strategy_version_pair(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha")
    with pytest.raises(sqlite3.IntegrityError), db.get_connection(db_path, write=True) as conn:
        db.create_version_row(
            conn,
            strategy_id="s1",
            version=1,
            parameters={},
            hash_=HASH,
            created_at_ms=5,
            created_by=None,
        )


def test_version_requires_existing_strategy(db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError), db.get_connection(db_path, write=True) as conn:
        db.create_version_row(
            conn,
            strategy_id="ghost",
            version=1,
            parameters={},
            hash_=HASH,
            created_at_ms=5,
            created_by=None,
        )


def test_versions_get_latest_and_list_ordered(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha", versions=3)
    with db.get_connection(db_path) as conn:
        latest = db.get_latest_version(conn, "s1")
        second = db.get_version(conn, "s1", 2)
        missing = db.get_version(conn, "s1", 9)
        no_versions = db.get_latest_version(conn, "ghost")
        page = db.list_versions(conn, "s1", limit=2, offset=1)
        everything = db.list_versions(conn, "s1")
    assert latest is not None
    assert latest.version == 3
    assert second is not None
    assert second.parameters == {"n": 2}
    assert missing is None
    assert no_versions is None
    assert [v.version for v in page] == [2, 3]
    assert [v.version for v in everything] == [1, 2, 3]


def test_update_metadata_only_touches_given_fields(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha", tags=["a"])
    with db.get_connection(db_path, write=True) as conn:
        db.update_strategy_metadata(conn, "s1", description="new")
        db.update_strategy_metadata(conn, "s1", tags=[])
        strategy = db.get_strategy_row(conn, "s1")
    assert strategy is not None
    assert strategy.description == "new"
    assert strategy.tags == []


def test_soft_delete_sets_timestamp_once_and_keeps_versions(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha", versions=2)
    with db.get_connection(db_path, write=True) as conn:
        assert db.soft_delete_strategy(conn, "s1", 100) is True
        assert db.soft_delete_strategy(conn, "s1", 200) is False
        assert db.soft_delete_strategy(conn, "ghost", 200) is False
    with db.get_connection(db_path) as conn:
        strategy = db.get_strategy_row(conn, "s1")
        versions = db.list_versions(conn, "s1")
    assert strategy is not None
    assert strategy.deleted_at_ms == 100
    assert len(versions) == 2


def test_hard_delete_cascades_to_versions(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "alpha", versions=2)
    _make_strategy(db_path, "s2", "beta")
    with db.get_connection(db_path, write=True) as conn:
        assert db.hard_delete_strategy(conn, "s1") is True
        assert db.hard_delete_strategy(conn, "s1") is False
    with db.get_connection(db_path) as conn:
        assert db.get_strategy_row(conn, "s1") is None
        assert db.list_versions(conn, "s1") == []
        assert db.get_version(conn, "s2", 1) is not None


def test_list_rows_filters_pagination_and_total(db_path: Path) -> None:
    _make_strategy(db_path, "s1", "trend_follow", tags=["trend", "fx"], created_at_ms=10)
    _make_strategy(db_path, "s2", "Mean_Rev", tags=["fx"], created_at_ms=20, versions=2)
    _make_strategy(db_path, "s3", "pairs", tags=[], created_at_ms=30)
    with db.get_connection(db_path, write=True) as conn:
        db.soft_delete_strategy(conn, "s3", 99)

    with db.get_connection(db_path) as conn:
        everything, total = db.list_strategy_rows(conn, StrategyListFilters())
        with_deleted, total_deleted = db.list_strategy_rows(
            conn, StrategyListFilters(include_deleted=True)
        )
        by_tag, tag_total = db.list_strategy_rows(conn, StrategyListFilters(tag="fx"))
        partial_tag, partial_total = db.list_strategy_rows(conn, StrategyListFilters(tag="tre"))
        by_name, name_total = db.list_strategy_rows(conn, StrategyListFilters(name_contains="REV"))
        wildcard, wildcard_total = db.list_strategy_rows(
            conn, StrategyListFilters(name_contains="%")
        )
        page, page_total = db.list_strategy_rows(conn, StrategyListFilters(limit=1, offset=1))

    assert [s.id for s in everything] == ["s2", "s1"]  # created_at_ms DESC, sin borradas
    assert total == 2
    assert [s.id for s in with_deleted] == ["s3", "s2", "s1"]
    assert total_deleted == 3
    assert [s.id for s in by_tag] == ["s2", "s1"]
    assert tag_total == 2
    assert (partial_tag, partial_total) == ([], 0)
    assert [s.id for s in by_name] == ["s2"]
    assert name_total == 1
    assert (wildcard, wildcard_total) == ([], 0)  # % se escapa, no es comodín
    assert [s.id for s in page] == ["s1"]
    assert page_total == 2  # total no depende de la paginación
    latest = everything[0]
    assert latest.latest_version == 2
    assert latest.hash_latest == f"{2:064x}"
    assert latest.updated_at_ms == 22
    assert latest.tags == ["fx"]
    deleted = with_deleted[0]
    assert deleted.deleted_at_ms == 99


def test_resolve_db_path_defaults_to_share_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(db.DB_PATH_ENV, raising=False)
    assert db.resolve_db_path() == (tmp_path / "share" / "strategies.db").resolve()


def test_resolve_db_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(db.DB_PATH_ENV, "custom/s.db")
    assert db.resolve_db_path() == (tmp_path / "custom" / "s.db").resolve()
    absolute = tmp_path / "abs" / "s.db"
    monkeypatch.setenv(db.DB_PATH_ENV, str(absolute))
    assert db.resolve_db_path() == absolute
