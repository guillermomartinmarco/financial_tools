"""Utilidades puras de ``ft_strategy_manager`` (T0.3, T6.1)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from ft_strategy_manager.errors import StrategyValidationError
from ft_strategy_manager.utils import (
    MASK,
    apply_json_patch,
    canonical_json,
    check_parameter_schema,
    compute_diff,
    compute_hash,
    mask_secrets,
    validate_parameters,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"window": {"type": "integer", "minimum": 1}, "mode": {"enum": ["a", "b"]}},
    "required": ["window"],
}


def test_canonical_json_sorts_keys_and_has_no_whitespace() -> None:
    assert canonical_json({"b": 1, "a": {"d": [1, 2], "c": "é"}}) == (
        '{"a":{"c":"é","d":[1,2]},"b":1}'
    )


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json({"a": float("nan")})


def test_hash_is_deterministic_and_order_independent() -> None:
    first = compute_hash({"a": 1, "b": [1, 2]})
    assert first == compute_hash({"b": [1, 2], "a": 1})
    assert first == hashlib.sha256(b'{"a":1,"b":[1,2]}').hexdigest()
    assert len(first) == 64


def test_hash_changes_with_content_and_type() -> None:
    assert compute_hash({"a": 1}) != compute_hash({"a": 2})
    assert compute_hash({"a": 1}) != compute_hash({"a": 1.0})
    assert compute_hash({"a": 1}) != compute_hash({"a": True})


def test_validate_without_schema_is_permissive() -> None:
    assert validate_parameters({"anything": [1]}, None) == (True, None)


def test_validate_accepts_conforming_parameters() -> None:
    assert validate_parameters({"window": 5, "mode": "a"}, SCHEMA) == (True, None)


def test_validate_reports_all_errors_with_path() -> None:
    ok, detail = validate_parameters({"window": 0, "mode": "z"}, SCHEMA)
    assert ok is False
    assert detail is not None
    assert "['window']" in detail
    assert "['mode']" in detail


def test_validate_reports_missing_required() -> None:
    ok, detail = validate_parameters({}, SCHEMA)
    assert ok is False
    assert detail is not None
    assert "'window' is a required property" in detail


def test_check_parameter_schema_rejects_invalid_schema() -> None:
    check_parameter_schema(SCHEMA)
    with pytest.raises(StrategyValidationError, match="Invalid parameter_schema"):
        check_parameter_schema({"type": "not-a-type"})


def test_apply_json_patch_is_pure_and_correct() -> None:
    original = {"a": 1, "b": {"c": 2}}
    patched = apply_json_patch(
        original,
        [
            {"op": "replace", "path": "/a", "value": 9},
            {"op": "add", "path": "/b/d", "value": [1]},
            {"op": "remove", "path": "/b/c"},
        ],
    )
    assert patched == {"a": 9, "b": {"d": [1]}}
    assert original == {"a": 1, "b": {"c": 2}}


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "remove", "path": "/missing"}],
        [{"op": "bogus", "path": "/a"}],
        [{"op": "test", "path": "/a", "value": 123}],
        [{"op": "add", "path": "/a/b/c", "value": 1}],
        [{"op": "replace", "path": "", "value": [1]}],
    ],
)
def test_apply_json_patch_rejects_bad_patches(patch: list[dict[str, Any]]) -> None:
    with pytest.raises(StrategyValidationError, match="Invalid parameters_patch"):
        apply_json_patch({"a": 1}, patch)


def test_compute_diff_added_removed_changed() -> None:
    diff = compute_diff({"a": 1, "b": 2, "c": [1]}, {"a": 1, "c": [2], "d": "x"}, 1, 3)
    assert diff.from_version == 1
    assert diff.to_version == 3
    assert diff.added == {"d": "x"}
    assert diff.removed == {"b": 2}
    assert diff.changed == {"c": ([1], [2])}


def test_compute_diff_preserves_json_types() -> None:
    diff = compute_diff({"a": 1, "b": 1, "c": "1"}, {"a": 1.0, "b": True, "c": 1})
    assert set(diff.changed) == {"a", "b", "c"}


def test_compute_diff_identical_is_empty() -> None:
    diff = compute_diff({"a": {"x": 1}}, {"a": {"x": 1}})
    assert (diff.added, diff.removed, diff.changed) == ({}, {}, {})


def test_mask_secrets_is_recursive() -> None:
    masked = mask_secrets(
        {
            "api_key": "k",
            "nested": {"Password": "p", "keep": 1, "list": [{"token": "t"}, 2]},
            "pair": ("x",),
        }
    )
    assert masked == {
        "api_key": MASK,
        "nested": {"Password": MASK, "keep": 1, "list": [{"token": MASK}, 2]},
        "pair": ["x"],
    }
