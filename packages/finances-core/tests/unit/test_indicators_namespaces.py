"""T1.6 (ft_indicators): espacios de nombres por herramienta (``talib_`` reservado)."""

from __future__ import annotations

import pytest

from finances_core.indicators_store.namespaces import (
    NAMESPACES,
    NamespaceViolationError,
    belongs_to,
    lock_file_name,
    manifest_file_name,
    validate_series_id,
)


def test_both_namespaces_registered() -> None:
    assert set(NAMESPACES) == {"ft_indicators", "ft_ta_lib"}
    assert NAMESPACES["ft_indicators"].prefix == ""
    assert NAMESPACES["ft_ta_lib"].prefix == "talib_"


@pytest.mark.parametrize("sid", ["rsi_14", "macd_12_26_9", "sma_50_adj_close", "obv"])
def test_ft_indicators_accepts_own_ids(sid: str) -> None:
    assert validate_series_id("ft_indicators", sid) == sid
    assert belongs_to("ft_indicators", sid)
    assert not belongs_to("ft_ta_lib", sid)


@pytest.mark.parametrize("sid", ["talib_rsi_14", "talib_cdldoji"])
def test_ft_ta_lib_accepts_own_ids(sid: str) -> None:
    assert validate_series_id("ft_ta_lib", sid) == sid
    assert belongs_to("ft_ta_lib", sid)
    assert not belongs_to("ft_indicators", sid)


@pytest.mark.parametrize(
    ("namespace", "sid"),
    [
        ("ft_indicators", "talib_rsi_14"),
        ("ft_ta_lib", "rsi_14"),
        ("ft_ta_lib", "talib_"),
        ("ft_indicators", "RSI_14"),
        ("ft_indicators", "rsi-14"),
        ("ft_indicators", ""),
        ("ft_indicators", "../rsi"),
    ],
)
def test_each_namespace_rejects_foreign_or_unsafe_ids(namespace: str, sid: str) -> None:
    with pytest.raises(NamespaceViolationError) as exc:
        validate_series_id(namespace, sid)
    assert isinstance(exc.value, ValueError)
    assert namespace in str(exc.value)


def test_unknown_namespace() -> None:
    with pytest.raises(NamespaceViolationError, match="desconocido"):
        validate_series_id("ft_other", "x")


def test_file_names_per_namespace() -> None:
    assert manifest_file_name("ft_indicators") == "_manifest.ft_indicators.json"
    assert manifest_file_name("ft_ta_lib") == "_manifest.ft_ta_lib.json"
    assert lock_file_name("ft_indicators") == "_lock.ft_indicators"
    assert lock_file_name("ft_ta_lib") == "_lock.ft_ta_lib"
