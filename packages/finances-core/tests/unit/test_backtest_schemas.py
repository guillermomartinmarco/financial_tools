"""B1.1: esquemas de ``finances_core.schemas.backtest`` (RF-1, RF-3, RF-5, RF-7, RF-9, RF-18)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from finances_core.schemas.backtest import (
    BacktestDataset,
    BacktestDispatchResponse,
    BacktestRequest,
    BacktestTaskResult,
    CostModel,
    EquityPage,
    KindInfo,
    ResolvedStrategy,
    RunListItem,
    RunManifest,
    RunsPage,
    RunSummary,
    TradesPage,
)

STRATEGY_ID = "01890a5d-ac96-774b-bcce-b302099a8057"
HASH = "a" * 64
DATASET = {"exchange": "binance", "market_type": "spot", "symbol": "BTCUSDT", "timeframe": "1h"}


def _request(**overrides: Any) -> BacktestRequest:
    body: dict[str, Any] = {"dataset": DATASET, "strategy_id": STRATEGY_ID}
    body.update(overrides)
    return BacktestRequest.model_validate(body)


def _resolved(**overrides: Any) -> ResolvedStrategy:
    body: dict[str, Any] = {
        "strategy_id": STRATEGY_ID,
        "name": "alpha",
        "version": 1,
        "hash": HASH,
        "kind": "sma_cross",
        "params": {"fast": 10, "slow": 30},
        "kind_version": "1.0.0",
        "code_hash": "b" * 64,
        "origin": "builtin",
    }
    body.update(overrides)
    return ResolvedStrategy.model_validate(body)


def _summary() -> RunSummary:
    return RunSummary(
        total_return=0.1,
        cagr=0.2,
        max_drawdown=-0.05,
        sharpe=None,
        sortino=None,
        win_rate=None,
        n_trades=0,
        exposure=0.0,
        initial_cash=10_000.0,
        final_equity=11_000.0,
        n_bars=10,
    )


# ----------------------------------------------------------------- BacktestRequest
def test_request_defaults_follow_rf3() -> None:
    request = _request()
    assert request.strategy_version is None
    assert request.start_ms is None
    assert request.end_ms is None
    assert request.initial_cash == 10_000.0
    assert request.size_fraction == 1.0
    assert request.fees_bps == 10.0
    assert request.slippage_bps == 0.0
    assert request.force is False
    assert request.dataset.symbol == "BTCUSDT"


def test_request_accepts_inclusive_equal_range() -> None:
    request = _request(start_ms=1_000, end_ms=1_000)
    assert (request.start_ms, request.end_ms) == (1_000, 1_000)


def test_request_rejects_start_after_end() -> None:
    with pytest.raises(ValidationError, match="start_ms"):
        _request(start_ms=2_000, end_ms=1_000)


@pytest.mark.parametrize("fraction", [0.0, -0.1, 1.0001, 2.0])
def test_request_rejects_size_fraction_outside_half_open_unit(fraction: float) -> None:
    with pytest.raises(ValidationError, match="size_fraction"):
        _request(size_fraction=fraction)


def test_request_accepts_size_fraction_upper_bound() -> None:
    assert _request(size_fraction=1.0).size_fraction == 1.0
    assert _request(size_fraction=0.01).size_fraction == 0.01


@pytest.mark.parametrize("field", ["fees_bps", "slippage_bps"])
def test_request_rejects_negative_costs(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _request(**{field: -1.0})


@pytest.mark.parametrize("cash", [0.0, -5.0, float("inf"), float("nan")])
def test_request_rejects_non_positive_or_non_finite_cash(cash: float) -> None:
    with pytest.raises(ValidationError, match="initial_cash"):
        _request(initial_cash=cash)


def test_request_accepts_zero_costs() -> None:
    request = _request(fees_bps=0.0, slippage_bps=0.0)
    assert (request.fees_bps, request.slippage_bps) == (0.0, 0.0)


@pytest.mark.parametrize(
    "key",
    ["code", "path", "module", "strategies_dir", "datasets", "strategies", "unknown"],
)
def test_request_forbids_extra_keys(key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        _request(**{key: "x"})


def test_request_forbids_extra_keys_inside_dataset() -> None:
    with pytest.raises(ValidationError, match="path"):
        _request(dataset={**DATASET, "path": "/etc"})


def test_request_has_no_code_path_or_module_fields() -> None:
    forbidden = {"code", "path", "module", "strategies_dir", "module_name", "file"}
    assert forbidden.isdisjoint(BacktestRequest.model_fields)


@pytest.mark.parametrize("strategy_id", ["not-a-uuid", "", "01890a5d-ac96-474b-bcce-b302099a8057"])
def test_request_rejects_non_uuid7_strategy_id(strategy_id: str) -> None:
    with pytest.raises(ValidationError, match="strategy_id"):
        _request(strategy_id=strategy_id)


def test_request_rejects_version_below_one() -> None:
    with pytest.raises(ValidationError, match="strategy_version"):
        _request(strategy_version=0)


def test_request_json_roundtrip_matches_request_file_body() -> None:
    request = _request(start_ms=0, end_ms=10, strategy_version=2, force=True)
    assert BacktestRequest.model_validate_json(request.model_dump_json()) == request


# ------------------------------------------------------------- ResolvedStrategy
def test_resolved_strategy_carries_kind_version_code_hash_and_origin() -> None:
    resolved = _resolved(origin="local")
    assert resolved.kind_version == "1.0.0"
    assert resolved.code_hash == "b" * 64
    assert resolved.origin == "local"


@pytest.mark.parametrize(
    ("field", "value"),
    [("origin", "remote"), ("hash", "xyz"), ("code_hash", "B" * 64), ("kind", "X")],
)
def test_resolved_strategy_rejects_invalid_fields(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _resolved(**{field: value})


def test_resolved_strategy_keeps_param_scalar_types() -> None:
    resolved = _resolved(params={"n": 3, "x": 1.5, "flag": True, "mode": "a"})
    assert resolved.params == {"n": 3, "x": 1.5, "flag": True, "mode": "a"}
    assert type(resolved.params["flag"]) is bool
    assert type(resolved.params["n"]) is int


# ----------------------------------------------------------------- KindInfo / costes
def test_kind_info_valid_and_invalid_shapes() -> None:
    ok = KindInfo(
        kind="sma_cross",
        version="1.0.0",
        origin="builtin",
        description="Cruce de medias.",
        code_hash="c" * 64,
        params_schema={"type": "object"},
        default_params={"fast": 10},
        metrics_target=None,
        valid=True,
        error=None,
    )
    bad = KindInfo(
        kind="broken",
        version=None,
        origin="local",
        description=None,
        code_hash=None,
        params_schema=None,
        default_params=None,
        metrics_target=None,
        valid=False,
        error="logic.py: falta",
    )
    assert ok.valid
    assert not bad.valid
    assert bad.error == "logic.py: falta"


@pytest.mark.parametrize("field", ["fees_bps", "slippage_bps"])
def test_cost_model_rejects_negative(field: str) -> None:
    body = {"fees_bps": 1.0, "slippage_bps": 1.0, field: -0.5}
    with pytest.raises(ValidationError, match=field):
        CostModel.model_validate(body)


# ----------------------------------------------- manifiesto, resumen, páginas, despacho
def test_manifest_summary_pages_and_dispatch_roundtrip() -> None:
    manifest = RunManifest(
        run_id="d" * 64,
        engine="vectorbt",
        engine_version="1.2.0",
        catalog_version="1.0.0",
        result_schema_version="1",
        request=_request(),
        strategy=_resolved(),
        start_ms=0,
        end_ms=10,
        n_bars=11,
        data_hash="e" * 64,
        data_gaps=0,
        created_at_ms=1,
        duration_ms=2,
        warnings=["insufficient_history"],
        python_version="3.12.0",
        libraries={"polars": "1.0"},
    )
    assert RunManifest.model_validate_json(manifest.model_dump_json()) == manifest
    result = BacktestTaskResult(run_id="d" * 64, cached=True, summary=_summary(), manifest=manifest)
    assert result.cached
    item = RunListItem(
        run_id="d" * 64,
        engine="vectorbt",
        strategy_id=STRATEGY_ID,
        strategy_version=1,
        kind="sma_cross",
        dataset=manifest.request.dataset,
        created_at_ms=1,
        summary=_summary(),
    )
    page = RunsPage(items=[item], count=1, next_cursor=None)
    assert page.items[0].kind == "sma_cross"
    assert TradesPage(items=[], count=0, next_after_ms=None).count == 0
    assert EquityPage(items=[{"equity": 1.0}], count=1, next_after_ms=5).next_after_ms == 5
    dispatch = BacktestDispatchResponse(task_id="t", poll_url="/s/t", result_url="/r/t")
    assert dispatch.status == "ACCEPTED"


def test_dispatch_status_is_fixed() -> None:
    with pytest.raises(ValidationError, match="status"):
        BacktestDispatchResponse(task_id="t", status="DONE", poll_url="/s", result_url="/r")  # type: ignore[arg-type]


# Enteros y booleanos estrictos: sin coerción desde textos ni bool, y rango Int64 (RF-3).
@pytest.mark.parametrize(
    "overrides",
    [
        {"start_ms": "5"},
        {"end_ms": "5"},
        {"strategy_version": True},
        {"strategy_version": "2"},
        {"force": "yes"},
        {"force": 1},
        {"start_ms": 2**63},
        {"end_ms": -(2**63) - 1},
    ],
)
def test_request_rejects_coerced_or_out_of_int64_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _request(**overrides)


def test_request_accepts_int64_bounds() -> None:
    request = _request(start_ms=-(2**63), end_ms=2**63 - 1)
    assert (request.start_ms, request.end_ms) == (-(2**63), 2**63 - 1)


@pytest.mark.parametrize("field", ["exchange", "symbol", "timeframe"])
@pytest.mark.parametrize(
    "value",
    [
        "..",
        ".",
        "../../etc",
        "a/b",
        "a\\b",
        "/etc",
        "\\\\server\\share",
        "C:",
        "C:\\x",
        "",
        "a\x00b",
        "a\nb",
        "a\x7fb",
    ],
)
def test_dataset_rejects_path_traversal_components(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        BacktestDataset.model_validate({**DATASET, field: value})


def test_request_rejects_path_traversal_inside_dataset() -> None:
    with pytest.raises(ValidationError, match="symbol"):
        _request(dataset={**DATASET, "symbol": "../../etc"})


@pytest.mark.parametrize("symbol", ["BTCUSDT", "BTC-USD", "^GSPC", "EURUSD=X", "BRK.B", "ES=F"])
def test_dataset_accepts_real_symbols(symbol: str) -> None:
    assert BacktestDataset.model_validate({**DATASET, "symbol": symbol}).symbol == symbol
