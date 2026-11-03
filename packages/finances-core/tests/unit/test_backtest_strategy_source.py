"""B1.5: ``finances_core.backtest.strategy_source`` sobre una ``strategies.db`` temporal (RF-3).

La base la crea ``ft_strategy_manager`` bajo ``FT_PROJECT_ROOT`` temporal (conftest); las
estrategias locales viven en ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backtest_helpers import write_strategy

from finances_core.backtest import strategy_source
from finances_core.backtest.catalog import StrategyRegistry, build_registry
from finances_core.backtest.errors import (
    InvalidParamsError,
    StrategyInitError,
    StrategyNotFoundError,
    StrategyVersionNotFoundError,
    UnknownKindError,
)
from finances_core.backtest.strategies.hashing import code_hash
from finances_core.schemas.strategy import StrategyCreate, StrategyUpdate
from ft_strategy_manager import service
from ft_strategy_manager.utils import compute_hash

MISSING_ID = "01890a5d-ac96-774b-bcce-b302099a8057"


@pytest.fixture
def local_dir(tmp_path: Path) -> Path:
    local = tmp_path / "local_strategies"
    write_strategy(local, "src_kind")
    write_strategy(local, "broken_kind", readme=None)
    return local


@pytest.fixture
def registry(tmp_path: Path, local_dir: Path) -> StrategyRegistry:
    builtin = tmp_path / "builtin"
    write_strategy(builtin, "core_kind")
    return build_registry(builtin_dir=builtin, local_dir=local_dir)


def _create(parameters: dict[str, Any], name: str = "alpha") -> str:
    return service.create_strategy(StrategyCreate(name=name, parameters=parameters)).strategy_id


def test_default_version_is_latest(registry: StrategyRegistry) -> None:
    strategy_id = _create({"kind": "src_kind", "params": {"window": 5}})
    service.update_strategy(
        strategy_id, StrategyUpdate(parameters={"kind": "src_kind", "params": {"window": 7}})
    )
    resolved = strategy_source.load(strategy_id, registry=registry)
    assert resolved.version == 2
    assert resolved.params == {"window": 7}
    assert resolved.name == "alpha"
    assert resolved.strategy_id == strategy_id


def test_explicit_version(registry: StrategyRegistry) -> None:
    strategy_id = _create({"kind": "src_kind", "params": {"window": 5}})
    service.update_strategy(
        strategy_id, StrategyUpdate(parameters={"kind": "src_kind", "params": {"window": 7}})
    )
    resolved = strategy_source.load(strategy_id, 1, registry=registry)
    assert resolved.version == 1
    assert resolved.params == {"window": 5}


def test_missing_strategy_maps_to_404(registry: StrategyRegistry) -> None:
    with pytest.raises(StrategyNotFoundError) as exc:
        strategy_source.load(MISSING_ID, registry=registry)
    assert (exc.value.code, exc.value.exit_code, exc.value.http_status) == (
        "strategy_not_found",
        1,
        404,
    )
    assert exc.value.strategy_id == MISSING_ID


def test_missing_version_maps_to_404(registry: StrategyRegistry) -> None:
    strategy_id = _create({"kind": "src_kind", "params": {}})
    with pytest.raises(StrategyVersionNotFoundError) as exc:
        strategy_source.load(strategy_id, 9, registry=registry)
    assert (exc.value.code, exc.value.exit_code, exc.value.http_status) == (
        "version_not_found",
        1,
        404,
    )
    assert exc.value.version == 9


def test_soft_deleted_strategy_is_not_found(registry: StrategyRegistry) -> None:
    strategy_id = _create({"kind": "src_kind", "params": {}})
    service.delete_strategy(strategy_id)
    with pytest.raises(StrategyNotFoundError) as exc:
        strategy_source.load(strategy_id, registry=registry)
    assert exc.value.http_status == 404
    with pytest.raises(StrategyNotFoundError):
        strategy_source.load(strategy_id, 1, registry=registry)


def test_unknown_kind_is_a_validation_error(registry: StrategyRegistry) -> None:
    strategy_id = _create({"kind": "nope_kind", "params": {}})
    with pytest.raises(UnknownKindError) as exc:
        strategy_source.load(strategy_id, registry=registry)
    assert exc.value.http_status == 400


def test_invalid_params_and_shape_are_validation_errors(registry: StrategyRegistry) -> None:
    bad_params = _create({"kind": "src_kind", "params": {"window": 1}}, name="bad-params")
    with pytest.raises(InvalidParamsError, match="params.window"):
        strategy_source.load(bad_params, registry=registry)
    bad_shape = _create({"window": 3}, name="bad-shape")
    with pytest.raises(InvalidParamsError, match="parameters.kind"):
        strategy_source.load(bad_shape, registry=registry)


def test_invalid_local_kind_raises_strategy_init_error(registry: StrategyRegistry) -> None:
    strategy_id = _create({"kind": "broken_kind", "params": {}})
    with pytest.raises(StrategyInitError):
        strategy_source.load(strategy_id, registry=registry)


def test_hash_and_registry_fields(registry: StrategyRegistry, local_dir: Path) -> None:
    parameters = {"kind": "src_kind", "params": {"window": 4}}
    strategy_id = _create(parameters)
    version = service.get_strategy(strategy_id).selected
    assert version is not None
    resolved = strategy_source.load(strategy_id, registry=registry)
    assert resolved.hash == version.hash == compute_hash(parameters)
    assert resolved.kind == "src_kind"
    assert resolved.kind_version == registry.get("src_kind").version
    assert (
        resolved.code_hash == registry.code_hashes["src_kind"] == code_hash(local_dir / "src_kind")
    )
    assert resolved.origin == "local"


def test_builtin_origin_comes_from_registry(registry: StrategyRegistry) -> None:
    strategy_id = _create({"kind": "core_kind", "params": {}})
    resolved = strategy_source.load(strategy_id, registry=registry)
    assert resolved.origin == "builtin"
    assert resolved.params == {"window": 3}  # defecto de Params


def test_default_registry_reads_ft_strategies_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_strategy(tmp_path / "env_strategies", "env_kind")
    monkeypatch.setenv("FT_STRATEGIES_DIR", str(tmp_path / "env_strategies"))
    strategy_id = _create({"kind": "env_kind", "params": {"window": 2}})
    resolved = strategy_source.load(strategy_id)
    assert (resolved.kind, resolved.origin) == ("env_kind", "local")
