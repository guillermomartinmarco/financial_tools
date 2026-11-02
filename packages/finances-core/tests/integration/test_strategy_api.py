"""Tests de integración del router ``/strategies`` (T5.1-T5.4, T6.5)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
import structlog
import yaml
from fastapi import FastAPI

from ft_strategy_manager.api import router

UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
GHOST = "00000000-0000-7000-8000-000000000000"


@pytest.fixture(autouse=True)
def _default_structlog() -> None:
    structlog.reset_defaults()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


async def _create(client: httpx.AsyncClient, name: str = "alpha", **fields: Any) -> dict[str, Any]:
    body = {"name": name, "parameters": {"window": 10, "mode": "a"}, **fields}
    response = await client.post("/strategies", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


# ------------------------------------------------------------------------- OpenAPI
async def test_openapi_lists_the_eight_endpoints() -> None:
    async with _client() as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    found = {path: set(ops) for path, ops in paths.items() if path.startswith("/strategies")}
    assert found == {
        "/strategies": {"post", "get"},
        "/strategies/{strategy_id}": {"get", "put", "delete"},
        "/strategies/{strategy_id}/versions": {"get"},
        "/strategies/{strategy_id}/diff": {"get"},
        "/strategies/{strategy_id}/export": {"get"},
    }


def test_route_names_are_unique_and_prefixed() -> None:
    names = [r.name for r in router.routes if hasattr(r, "name")]
    assert len(names) == len(set(names)) == 8
    assert all(n.startswith("strategies_") for n in names)


# --------------------------------------------------------------------------- create
async def test_create_returns_201_location_and_version_body() -> None:
    async with _client() as client:
        response = await client.post(
            "/strategies",
            json={"name": "alpha", "parameters": {"window": 10}, "tags": ["fx"]},
            headers={"X-User-ID": "ana"},
        )
    assert response.status_code == 201
    body = response.json()
    assert UUID7_RE.match(body["strategy_id"])
    assert response.headers["Location"] == f"/strategies/{body['strategy_id']}"
    assert body["version"] == 1
    assert len(body["hash"]) == 64
    assert body["created_by"] == "ana"
    assert body["parameters"] == {"window": 10}
    assert body["created_at_ms"] > 0


async def test_create_conflict_and_validation_errors() -> None:
    async with _client() as client:
        await _create(client, "alpha")
        dup = await client.post("/strategies", json={"name": "alpha", "parameters": {}})
        bad_name = await client.post("/strategies", json={"name": "Bad Name"})
        no_name = await client.post("/strategies", json={"parameters": {}})
        bad_schema = await client.post(
            "/strategies",
            json={
                "name": "beta",
                "parameters": {"w": 0},
                "parameter_schema": {"properties": {"w": {"minimum": 1}}},
            },
        )
        invalid_schema = await client.post(
            "/strategies",
            json={"name": "gamma", "parameters": {}, "parameter_schema": {"type": "bogus"}},
        )
        listing = await client.get("/strategies")
    assert dup.status_code == 409
    assert "alpha" in dup.json()["detail"]
    assert bad_name.status_code == 422
    assert isinstance(bad_name.json()["detail"], list)  # detail estructurado de Pydantic
    assert no_name.status_code == 422
    assert bad_schema.status_code == 422
    assert "w" in bad_schema.json()["detail"]
    assert invalid_schema.status_code == 422
    assert listing.json()["total_count"] == 1


# ------------------------------------------------------------------------------ get
async def test_get_latest_specific_version_and_not_found() -> None:
    async with _client() as client:
        sid = (await _create(client))["strategy_id"]
        await client.put(f"/strategies/{sid}", json={"parameters": {"window": 20}})
        latest = (await client.get(f"/strategies/{sid}")).json()
        first = (await client.get(f"/strategies/{sid}", params={"version": 1})).json()
        missing_strategy = await client.get(f"/strategies/{GHOST}")
        missing_version = await client.get(f"/strategies/{sid}", params={"version": 9})
        bad_version = await client.get(f"/strategies/{sid}", params={"version": 0})
        bad_id = await client.get("/strategies/not-a-uuid")
    assert latest["selected_version"] == 2
    assert [v["version"] for v in latest["versions"]] == [1, 2]
    assert first["selected_version"] == 1
    assert missing_strategy.status_code == 404
    assert missing_strategy.json() == {"detail": "Strategy not found"}
    assert missing_version.status_code == 404
    assert bad_version.status_code == 422
    assert bad_id.status_code == 422


# ---------------------------------------------------------------------------- update
async def test_update_replace_patch_and_errors() -> None:
    async with _client() as client:
        sid = (await _create(client))["strategy_id"]
        replaced = await client.put(
            f"/strategies/{sid}",
            json={"parameters": {"window": 20}, "description": "d", "tags": ["x"]},
            headers={"X-User-ID": "bo"},
        )
        patched = await client.put(
            f"/strategies/{sid}",
            json={"parameters_patch": [{"op": "add", "path": "/extra", "value": [1]}]},
        )
        same = await client.put(
            f"/strategies/{sid}", json={"parameters": {"window": 20, "extra": [1]}}
        )
        bad_patch = await client.put(
            f"/strategies/{sid}", json={"parameters_patch": [{"op": "remove", "path": "/zzz"}]}
        )
        both = await client.put(
            f"/strategies/{sid}", json={"parameters": {}, "parameters_patch": []}
        )
        neither = await client.put(f"/strategies/{sid}", json={})
        ghost = await client.put(f"/strategies/{GHOST}", json={"parameters": {"a": 1}})
        strategy = (await client.get(f"/strategies/{sid}")).json()
    assert replaced.status_code == 200
    assert replaced.json()["version"] == 2
    assert replaced.json()["created_by"] == "bo"
    assert patched.json()["version"] == 3
    assert patched.json()["parameters"] == {"window": 20, "extra": [1]}
    assert same.status_code == 400
    assert same.json() == {"detail": "No changes detected"}
    assert bad_patch.status_code == 422
    assert "Invalid parameters_patch" in bad_patch.json()["detail"]
    assert both.status_code == 422
    assert neither.status_code == 422
    assert ghost.status_code == 404
    assert strategy["description"] == "d"
    assert strategy["tags"] == ["x"]
    assert len(strategy["versions"]) == 3


# ---------------------------------------------------------------------------- delete
async def test_delete_soft_purge_and_idempotency() -> None:
    async with _client() as client:
        sid = (await _create(client))["strategy_id"]
        soft = await client.delete(f"/strategies/{sid}")
        listed = (await client.get("/strategies")).json()["total_count"]
        with_deleted = (await client.get("/strategies", params={"include_deleted": True})).json()
        still_there = await client.get(f"/strategies/{sid}")
        update_deleted = await client.put(f"/strategies/{sid}", json={"parameters": {"a": 1}})
        purged = await client.delete(f"/strategies/{sid}", params={"purge": True})
        gone = await client.get(f"/strategies/{sid}")
        again = await client.delete(f"/strategies/{sid}", params={"purge": True})
        never = await client.delete(f"/strategies/{GHOST}")
    assert soft.status_code == 204
    assert soft.content == b""
    assert listed == 0
    assert with_deleted["total_count"] == 1
    assert with_deleted["items"][0]["deleted_at_ms"] is not None
    assert still_there.json()["deleted_at_ms"] is not None
    assert update_deleted.status_code == 404
    assert purged.status_code == 204
    assert gone.status_code == 404
    assert again.status_code == 204
    assert never.status_code == 204


# ------------------------------------------------------------------------------ list
async def test_list_filters_pagination_and_limits() -> None:
    async with _client() as client:
        await _create(client, "trend_follow", tags=["fx"])
        await _create(client, "mean_rev", parameters={"w": 2})
        everything = (await client.get("/strategies")).json()
        by_tag = (await client.get("/strategies", params={"tag": "fx"})).json()
        by_name = (await client.get("/strategies", params={"name": "REV"})).json()
        page = (await client.get("/strategies", params={"limit": 1, "offset": 1})).json()
        too_big = await client.get("/strategies", params={"limit": 501})
        negative = await client.get("/strategies", params={"offset": -1})
    assert everything["total_count"] == 2
    assert (everything["limit"], everything["offset"]) == (50, 0)
    assert [i["name"] for i in by_tag["items"]] == ["trend_follow"]
    assert [i["name"] for i in by_name["items"]] == ["mean_rev"]
    assert len(page["items"]) == 1
    assert page["total_count"] == 2
    item = everything["items"][0]
    assert set(item) == {
        "id",
        "name",
        "latest_version",
        "updated_at_ms",
        "tags",
        "hash_latest",
        "deleted_at_ms",
    }
    assert too_big.status_code == 422
    assert negative.status_code == 422


# ----------------------------------------------------------------- versions / diff
async def test_versions_endpoint_pagination_and_not_found() -> None:
    async with _client() as client:
        sid = (await _create(client))["strategy_id"]
        await client.put(f"/strategies/{sid}", json={"parameters": {"w": 1}})
        await client.put(f"/strategies/{sid}", json={"parameters": {"w": 2}})
        everything = (await client.get(f"/strategies/{sid}/versions")).json()
        page = await client.get(f"/strategies/{sid}/versions", params={"limit": 1, "offset": 2})
        ghost = await client.get(f"/strategies/{GHOST}/versions")
    assert [v["version"] for v in everything] == [1, 2, 3]
    assert [v["version"] for v in page.json()] == [3]
    assert ghost.status_code == 404


async def test_diff_endpoint() -> None:
    async with _client() as client:
        sid = (await _create(client, parameters={"a": 1, "gone": True}))["strategy_id"]
        await client.put(f"/strategies/{sid}", json={"parameters": {"a": 2, "added": "x"}})
        diff = await client.get(f"/strategies/{sid}/diff", params={"from": 1, "to": 2})
        backwards = await client.get(f"/strategies/{sid}/diff", params={"from": 2, "to": 1})
        missing = await client.get(f"/strategies/{sid}/diff", params={"from": 1, "to": 9})
        ghost = await client.get(f"/strategies/{GHOST}/diff", params={"from": 1, "to": 2})
        invalid = await client.get(f"/strategies/{sid}/diff", params={"from": 0, "to": 2})
        absent = await client.get(f"/strategies/{sid}/diff")
    assert diff.status_code == 200
    assert diff.json() == {
        "from_version": 1,
        "to_version": 2,
        "added": {"added": "x"},
        "removed": {"gone": True},
        "changed": {"a": [1, 2]},
    }
    assert backwards.json()["changed"] == {"a": [2, 1]}
    assert missing.status_code == 404
    assert ghost.status_code == 404
    assert invalid.status_code == 422
    assert absent.status_code == 422


# ---------------------------------------------------------------------------- export
async def test_export_json_and_yaml_as_attachment() -> None:
    async with _client() as client:
        sid = (await _create(client, description="d"))["strategy_id"]
        as_json = await client.get(f"/strategies/{sid}/export")
        as_yaml = await client.get(f"/strategies/{sid}/export", params={"format": "yaml"})
        bad_format = await client.get(f"/strategies/{sid}/export", params={"format": "xml"})
        ghost = await client.get(f"/strategies/{GHOST}/export")
    assert as_json.status_code == 200
    assert as_json.headers["content-type"] == "application/json"
    assert as_json.headers["content-disposition"] == f'attachment; filename="strategy-{sid}.json"'
    assert json.loads(as_json.content)["strategy"]["id"] == sid
    assert as_yaml.headers["content-type"] == "application/yaml"
    assert as_yaml.headers["content-disposition"].endswith('.yaml"')
    assert yaml.safe_load(as_yaml.content)["strategy"]["description"] == "d"
    assert bad_format.status_code == 422
    assert ghost.status_code == 404
