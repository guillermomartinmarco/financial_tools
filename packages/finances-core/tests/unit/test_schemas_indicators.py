"""T1.5 (ft_indicators): contratos genéricos, propios y nomenclatura RF-4."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.dispatch import DispatchResponse as CoreDispatchResponse
from finances_core.schemas.indicators import (
    EXPORT_ID_COLUMNS,
    OHLCV_EXPORT_COLUMNS,
    ComputeItemResult,
    ComputeProgress,
    ComputeResult,
    DispatchResponse,
    IndicatorCatalogEntry,
    IndicatorComputeRequest,
    IndicatorPlan,
    IndicatorPlanItem,
    IndicatorSpec,
    SeriesExportRequest,
    Stability,
    StoreEntry,
    StoreManifest,
    TalibEquivalentInfo,
    format_param,
    output_columns,
    series_id,
)

ID_RE = re.compile(r"^[a-z0-9_]+$")
DS = DatasetIdentifier(exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe="1h")


def _stability() -> Stability:
    return Stability(
        stable_from_ms=10,
        by_column={"rsi_14": 10},
        rule="wilder alpha=1/14 eps=1e-6 seed_idx=14 k=187",
        reason="converged",
    )


# --- nomenclatura --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "token"),
    [
        (14, "14"),
        (2.0, "2"),
        (2.5, "2p5"),
        (0.25, "0p25"),
        (-0.5, "m0p5"),
        (-3, "m3"),
        (1e-6, "0p000001"),
        (100.10, "100p1"),
        ("adj_close", "adj_close"),
        ("EMA", "ema"),
    ],
)
def test_format_param(value: int | float | str, token: str) -> None:
    assert format_param(value) == token


@pytest.mark.parametrize("value", [1e-7, 0.1234567, float("nan"), float("inf"), True, "a-b"])
def test_format_param_rejects_unrepresentable(value: object) -> None:
    with pytest.raises(ValueError, match="parámetro"):
        format_param(value)


def test_rf4_examples() -> None:
    assert output_columns(series_id("", "rsi", [14]), []) == ["rsi_14"]
    macd = series_id("", "macd", [12, 26, 9])
    assert output_columns(macd, ["line", "signal", "hist"]) == [
        "macd_12_26_9_line",
        "macd_12_26_9_signal",
        "macd_12_26_9_hist",
    ]
    bb = series_id("", "bb", [20, 2.5])
    assert output_columns(bb, ["middle", "upper", "lower"])[1] == "bb_20_2p5_upper"
    assert series_id("", "bb", [20, 2.0]) == "bb_20_2"
    assert series_id("", "sma", [50], ["adj_close"]) == "sma_50_adj_close"
    assert output_columns(series_id("", "adx", [14]), ["plus_di"]) == ["adx_14_plus_di"]


def test_talib_prefix() -> None:
    sid = series_id("talib_", "BBANDS", [20, 2.0, 2.0, "sma"])
    assert sid == "talib_bbands_20_2_2_sma"
    assert output_columns(sid, ["upperband"]) == ["talib_bbands_20_2_2_sma_upperband"]


@pytest.mark.parametrize(
    ("prefix", "name"),
    [("", "bad-name"), ("", ""), ("Talib-", "rsi"), ("", "rsi x")],
)
def test_series_id_rejects_unsafe(prefix: str, name: str) -> None:
    with pytest.raises(ValueError, match="identificador"):
        series_id(prefix, name, [14])


_params = st.lists(
    st.one_of(
        st.integers(min_value=-10_000, max_value=10_000),
        st.decimals(
            min_value=-1000, max_value=1000, places=2, allow_nan=False, allow_infinity=False
        ).map(float),
    ),
    min_size=0,
    max_size=4,
)
_sources = st.sampled_from([[], ["adj_close"], ["open"], ["high"]])


@given(st.sampled_from(["sma", "ema", "bb", "macd"]), _params, _sources)
def test_ids_are_safe(name: str, params: list[float], overrides: list[str]) -> None:
    sid = series_id("", name, params, overrides)
    assert ID_RE.fullmatch(sid)
    assert all(ID_RE.fullmatch(c) for c in output_columns(sid, ["upper", "plus_di"]))


@given(_params, _params, _sources, _sources)
def test_distinct_specs_never_collide(
    a: list[float], b: list[float], src_a: list[str], src_b: list[str]
) -> None:
    """Propiedad: con el mismo nombre y aridad, specs distintas dan ids distintos."""
    if len(a) != len(b):
        b = (b + a)[: len(a)]
    same_values = all(x == y for x, y in zip(a, b, strict=True))
    if same_values and src_a == src_b:
        return
    assert series_id("", "ind", a, src_a) != series_id("", "ind", b, src_b)


# --- contratos -----------------------------------------------------------------------


def test_dispatch_response_is_core_contract() -> None:
    assert DispatchResponse is CoreDispatchResponse


def test_polars_contract_constants() -> None:
    assert EXPORT_ID_COLUMNS == ["exchange", "market_type", "symbol", "timeframe", "open_time_ms"]
    assert "adj_close" in OHLCV_EXPORT_COLUMNS


def test_store_manifest_roundtrip_with_int16_columns() -> None:
    entry = StoreEntry(
        series_id="talib_cdldoji",
        spec={"function": "CDLDOJI", "params": {}},
        columns=["talib_cdldoji"],
        column_dtypes={"talib_cdldoji": "Int16"},
        engine_version="ta-lib 0.8.2 / C 0.8.1",
        rows=10,
        first_ms=0,
        last_ms=9,
        stability=Stability(
            stable_from_ms=None, by_column={}, rule="", reason="unstable_period_unknown"
        ),
        computed_at_ms=1,
    )
    manifest = StoreManifest(namespace="ft_ta_lib", dataset=DS, entries={entry.series_id: entry})
    assert StoreManifest.model_validate_json(manifest.model_dump_json()) == manifest
    assert manifest.lock_task_id is None


def test_store_entry_rejects_float64_dtype() -> None:
    with pytest.raises(ValidationError):
        StoreEntry(
            series_id="rsi_14",
            spec={},
            columns=["rsi_14"],
            column_dtypes={"rsi_14": "Float64"},
            engine_version="ft_indicators:1",
            rows=1,
            first_ms=None,
            last_ms=None,
            stability=_stability(),
            computed_at_ms=0,
        )


def test_progress_result_and_export_models() -> None:
    item = ComputeItemResult(
        dataset=DS,
        series_id="rsi_14",
        columns=["rsi_14"],
        rows=3,
        first_ms=1,
        last_ms=3,
        stability=_stability(),
        partitions_written=1,
        partitions_removed=0,
        warnings=[],
    )
    progress = ComputeProgress(
        task_id="t",
        status="COMPUTING",
        datasets_total=1,
        datasets_done=0,
        items_total=1,
        items_done=0,
        rows_computed=0,
        partitions_written=0,
        stability={"binance/spot/BTCUSDT/1h:rsi_14": _stability()},
        warnings=[],
        errors=[],
    )
    result = ComputeResult(
        task_id="t", status="COMPLETED", items=[item], output_dirs=[], warnings=[], errors=[]
    )
    assert ComputeResult.model_validate_json(result.model_dump_json()) == result
    assert progress.status == "COMPUTING"
    req = SeriesExportRequest()
    assert (req.stability, req.format, req.include_ohlcv) == ("all", "parquet", False)
    with pytest.raises(ValidationError):
        SeriesExportRequest(stability="maybe")


def test_indicator_request_limits() -> None:
    spec = IndicatorSpec(name="rsi")
    assert spec.params == {}
    req = IndicatorComputeRequest(datasets=[DS], indicators=[spec])
    assert req.indicators[0].name == "rsi"
    with pytest.raises(ValidationError):
        IndicatorComputeRequest(datasets=[], indicators=[spec])
    with pytest.raises(ValidationError):
        IndicatorComputeRequest(datasets=[DS], indicators=[spec] * 51)
    with pytest.raises(ValidationError):
        IndicatorComputeRequest(datasets=[DS] * 101, indicators=[spec])
    with pytest.raises(ValidationError):
        IndicatorSpec(name="kama")


def test_indicator_plan_and_catalog_models() -> None:
    plan = IndicatorPlan(
        task_id="t",
        created_at_ms=0,
        items=[
            IndicatorPlanItem(
                dataset=DS,
                indicator_id="rsi_14",
                spec=IndicatorSpec(name="rsi", params={"period": 14, "source": "close"}),
                columns=["rsi_14"],
            )
        ],
    )
    assert IndicatorPlan.model_validate_json(plan.model_dump_json()) == plan
    entry = IndicatorCatalogEntry(
        name="bb",
        params={"period": {"type": "int", "default": 20, "min": 2, "max": 10000}},
        inputs=["close"],
        outputs=["middle", "upper", "lower"],
        warmup="period-1",
        stable_from="first_non_null",
        f32_tolerance={"upper": {"rtol": 1e-5, "atol": 0.0}},
        talib_equivalent=TalibEquivalentInfo(
            function="BBANDS",
            params={"timeperiod": "period", "nbdevup": "std", "matype": "0"},
            outputs={"upper": "upperband"},
            compare_from="first_non_null",
        ),
        engine_version=1,
        convention="Bollinger, sigma poblacional",
    )
    assert entry.talib_equivalent is not None
    assert entry.talib_equivalent.excluded_rows is None
    stoch = TalibEquivalentInfo(function="STOCH", params={}, outputs={}, compare_from=17)
    assert stoch.compare_from == 17
