"""T1.2: validación de los esquemas de ``ft_market_data_manager``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from finances_core.schemas import (
    ImportRequest,
    ManagerRequest,
    ManagerResult,
    PruneRequest,
)

DEST: dict[str, Any] = {
    "source": "x.parquet",
    "exchange": "binance",
    "market_type": "spot",
    "symbol": "BTCUSDT",
    "timeframe": "1h",
}


def test_prune_before_ms_only_is_valid() -> None:
    assert PruneRequest(before_ms=1).all is False


def test_prune_all_with_dataset_filter_is_valid() -> None:
    assert PruneRequest(all=True, symbol=["BTCUSDT"]).all is True


def test_prune_requires_exactly_one_criterion() -> None:
    with pytest.raises(ValidationError):
        PruneRequest()
    with pytest.raises(ValidationError):
        PruneRequest(before_ms=1, all=True, symbol=["BTCUSDT"])


def test_prune_all_without_dataset_filter_is_invalid() -> None:
    with pytest.raises(ValidationError, match="filtro de dataset"):
        PruneRequest(all=True)
    with pytest.raises(ValidationError):
        PruneRequest(all=True, start_ms=0, end_ms=10)  # el rango no es filtro de dataset


def test_import_request_defaults() -> None:
    req = ImportRequest.model_validate(DEST)
    assert (req.ts_column, req.ts_unit, req.on_conflict) == ("open_time_ms", "ms", "skip")
    assert req.create is False
    assert req.format is None


@pytest.mark.parametrize("missing", ["exchange", "market_type", "symbol", "timeframe", "source"])
def test_import_request_requires_full_destination(missing: str) -> None:
    payload = {k: v for k, v in DEST.items() if k != missing}
    with pytest.raises(ValidationError):
        ImportRequest.model_validate(payload)


def test_import_request_rejects_empty_destination_and_bad_enums() -> None:
    with pytest.raises(ValidationError):
        ImportRequest.model_validate({**DEST, "symbol": ""})
    with pytest.raises(ValidationError):
        ImportRequest.model_validate({**DEST, "ts_unit": "h"})
    with pytest.raises(ValidationError):
        ImportRequest.model_validate({**DEST, "on_conflict": "merge"})


def test_manager_request_and_result_roundtrip() -> None:
    assert ManagerRequest().dry_run is False
    result = ManagerResult(action="compact", dry_run=True, partitions=[])
    assert ManagerResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        ManagerResult(action="nope", dry_run=True, partitions=[])  # type: ignore[arg-type]
