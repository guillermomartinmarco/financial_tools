"""T4.2 — Router ``/datasets``: 202 inmediato, 400/404/409/422 y rutas con nombres únicos."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from finances_core.config import FinancesSettings
from finances_core.datasets import recipes

from finances_downloader.ft_datasets import api, service

from .conftest import AAPL_KEY, RECIPE_NAME, UNKNOWN_TASK, FakeRunner, write_item

BASE = "/datasets"


@pytest.fixture
def fake() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(fake: FakeRunner) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_runner] = lambda: fake
    return TestClient(app)


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Sustituye la ejecución en segundo plano para comprobar el despacho inmediato."""
    calls: list[tuple[Any, ...]] = []

    def fake_run(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(service, "run_materialize_sync", fake_run)
    return calls


def test_router_exposes_eight_routes_with_unique_names() -> None:
    routes = [r for r in api.router.routes if isinstance(r, APIRoute)]
    assert {(r.path, tuple(r.methods), r.name) for r in routes} == {
        (f"{BASE}/recipes", ("GET",), "datasets_recipes"),
        (f"{BASE}/recipes/{{name}}", ("GET",), "datasets_recipe"),
        (f"{BASE}/materialize/{{name}}", ("POST",), "datasets_materialize"),
        (f"{BASE}/status/{{task_id}}", ("GET",), "datasets_status"),
        (f"{BASE}/plan/{{task_id}}", ("GET",), "datasets_plan"),
        (f"{BASE}/result/{{task_id}}", ("GET",), "datasets_result"),
        (f"{BASE}/verify/{{name}}", ("POST",), "datasets_verify"),
        (f"{BASE}/lock/{{name}}", ("POST",), "datasets_lock"),
    }


def test_materialize_returns_202_with_pending_task(
    client: TestClient, recipe_dir: Path, dispatched: list[tuple[Any, ...]]
) -> None:
    response = client.post(f"{BASE}/materialize/{RECIPE_NAME}", params={"refetch": True})
    assert response.status_code == 202
    body = response.json()
    task_id = body["task_id"]
    assert body["status"] == "ACCEPTED"
    assert body["poll_url"].endswith(f"{BASE}/status/{task_id}")
    assert body["plan_url"].endswith(f"{BASE}/plan/{task_id}")
    assert body["result_url"].endswith(f"{BASE}/result/{task_id}")
    status = client.get(f"{BASE}/status/{task_id}").json()
    assert status["status"] == "PENDING"
    assert status["recipe"] == RECIPE_NAME
    assert len(dispatched) == 1
    assert dispatched[0][1]["refetch"] is True
    assert dispatched[0][1]["verify"] is True
    response = client.get(f"{BASE}/result/{task_id}")
    assert response.status_code == 409


def test_materialize_runs_in_background_until_completed(
    client: TestClient, recipe_dir: Path, fake: FakeRunner
) -> None:
    task_id = client.post(f"{BASE}/materialize/{RECIPE_NAME}").json()["task_id"]
    result = client.get(f"{BASE}/result/{task_id}")
    assert result.status_code == 200
    assert result.json()["outcome"] == "UNLOCKED"
    assert len(fake.calls) == 2
    plan = client.get(f"{BASE}/plan/{task_id}")
    assert plan.status_code == 200
    assert len(plan.json()["blocks"]) == 2


def test_400_on_invalid_recipe_or_lock(
    client: TestClient, recipe_dir: Path, dispatched: list[tuple[Any, ...]]
) -> None:
    (recipe_dir / "bad.toml").write_text("schema_version = 1\nname = 'bad'\n", encoding="utf-8")
    assert client.post(f"{BASE}/materialize/bad").status_code == 400
    assert client.post(f"{BASE}/verify/bad").status_code == 400
    (recipe_dir / f"{RECIPE_NAME}.lock.json").write_text("{}", encoding="utf-8")
    assert client.post(f"{BASE}/verify/{RECIPE_NAME}").status_code == 400
    assert dispatched == []


def test_404_for_unknown_recipe_and_task(client: TestClient, recipe_dir: Path) -> None:
    assert client.get(f"{BASE}/recipes/nope").status_code == 404
    assert client.post(f"{BASE}/materialize/nope").status_code == 404
    assert client.post(f"{BASE}/verify/nope").status_code == 404
    assert client.post(f"{BASE}/lock/nope").status_code == 404
    for route in ("status", "plan", "result"):
        assert client.get(f"{BASE}/{route}/{UNKNOWN_TASK}").status_code == 404


def test_422_for_invalid_name_or_task_id(client: TestClient, recipe_dir: Path) -> None:
    assert client.get(f"{BASE}/recipes/Bad Name").status_code == 422
    assert client.post(f"{BASE}/verify/..%2Fx").status_code in {404, 422}
    assert client.post(f"{BASE}/lock/.hidden").status_code == 422
    assert client.get(f"{BASE}/status/not-a-uuid").status_code == 422


def test_dotdot_name_never_reads_outside_recipes_dir(
    client: TestClient, recipe_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "x.toml"
    outside.write_text("schema_version = 1\nname = 'x'\n", encoding="utf-8")
    opened: list[Path] = []
    original = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    for path in (f"{BASE}/recipes/..%2Fx", f"{BASE}/recipes/%2E%2E%2Fx", f"{BASE}/verify/..x"):
        response = (client.post if "verify" in path else client.get)(path)
        assert response.status_code in {404, 422}, path
    assert outside not in opened
    assert all(p.resolve().is_relative_to(recipe_dir.resolve()) for p in opened)


def test_lock_409_when_rejected_and_200_otherwise(client: TestClient, recipe_dir: Path) -> None:
    missing = client.post(f"{BASE}/lock/{RECIPE_NAME}")
    assert missing.status_code == 409
    assert missing.json()["outcome"] == "REJECTED"

    settings = FinancesSettings()
    recipe = recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    for item in recipe.items():
        write_item(settings.DATASETS_DIR, item)
    assert client.post(f"{BASE}/lock/{RECIPE_NAME}").json()["outcome"] == "WRITTEN"
    assert client.post(f"{BASE}/lock/{RECIPE_NAME}").json()["outcome"] == "UNCHANGED"
    aapl = next(i for i in recipe.items() if i.key == AAPL_KEY)
    write_item(settings.DATASETS_DIR, aapl, scale=0.25)

    verify = client.post(f"{BASE}/verify/{RECIPE_NAME}")
    assert verify.status_code == 200
    assert verify.json()["status"] == "MISMATCH"
    rejected = client.post(f"{BASE}/lock/{RECIPE_NAME}")
    assert rejected.status_code == 409
    assert rejected.json()["differences"]
    updated = client.post(f"{BASE}/lock/{RECIPE_NAME}", params={"update": True})
    assert updated.status_code == 200
    assert updated.json()["outcome"] == "UPDATED"


def test_verify_409_on_non_canonical_data(client: TestClient, recipe_dir: Path) -> None:
    settings = FinancesSettings()
    recipe = recipes.load_recipe(settings.DATASETS_RECIPES_DIR, RECIPE_NAME)
    for item in recipe.items():
        write_item(settings.DATASETS_DIR, item)
    for path in (settings.DATASETS_DIR / "yfinance").rglob("*.parquet"):
        path.write_bytes(b"garbage")
    assert client.post(f"{BASE}/verify/{RECIPE_NAME}").status_code == 409


def test_recipes_listing(client: TestClient, recipe_dir: Path) -> None:
    body = client.get(f"{BASE}/recipes").json()
    assert [(r["name"], r["lock"]) for r in body] == [(RECIPE_NAME, "UNLOCKED")]
