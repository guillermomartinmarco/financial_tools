"""T4.1: planificación (resolución de specs/columnas por dataset) y persistencia del plan."""

from __future__ import annotations

import pytest
from finances_core.config import FinancesSettings
from finances_core.indicators_store.source import SourceNotFoundError
from finances_core.marketdata import plans
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    IndicatorComputeRequest,
    IndicatorPlan,
    IndicatorSpec,
)

from finances_analytics.ft_indicators import planner
from finances_analytics.ft_indicators.errors import IndicatorValidationError

from .conftest import DatasetFactory

TASK_ID = "0192f000-0000-7000-8000-000000000001"


def _request(datasets: list[DatasetIdentifier], *specs: IndicatorSpec) -> IndicatorComputeRequest:
    return IndicatorComputeRequest(datasets=datasets, indicators=list(specs))


def test_plan_has_one_item_per_dataset_and_deduplicated_indicator(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    btc = hive_dataset(symbol="BTCUSDT")
    aapl = hive_dataset(exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")
    request = _request(
        [btc, aapl, btc],
        IndicatorSpec(name="rsi"),
        IndicatorSpec(name="rsi", params={"period": 14}),  # mismo indicator_id
        IndicatorSpec(name="macd"),
    )
    plan = planner.plan_items(request, settings, task_id=TASK_ID, created_at_ms=123)

    assert plan.task_id == TASK_ID
    assert plan.created_at_ms == 123
    assert [(i.dataset, i.indicator_id) for i in plan.items] == [
        (btc, "rsi_14"),
        (btc, "macd_12_26_9"),
        (aapl, "rsi_14"),
        (aapl, "macd_12_26_9"),
    ]
    macd = plan.items[1]
    assert macd.columns == ["macd_12_26_9_line", "macd_12_26_9_signal", "macd_12_26_9_hist"]
    assert macd.spec == IndicatorSpec(
        name="macd", params={"fast": 12, "slow": 26, "signal": 9, "source": "close"}
    )


def test_plan_resolves_items_by_dataset(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    plan = planner.plan_items(
        _request([ds], IndicatorSpec(name="bb", params={"std": 2.5}), IndicatorSpec(name="obv")),
        settings,
        task_id=TASK_ID,
    )
    by_dataset = planner.items_by_dataset(plan)
    assert list(by_dataset) == [ds]
    assert [r.indicator_id for r in by_dataset[ds]] == ["bb_20_2p5", "obv"]
    assert by_dataset[ds][1].inputs == ("close", "volume")


def test_plan_persists_with_indicators_prefix(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    plan = planner.plan_items(_request([ds], IndicatorSpec(name="ema")), settings, task_id=TASK_ID)
    path = planner.save_plan(plan, settings)

    assert path == settings.SHARE_DIR / "plans" / f"indicators_plan_{TASK_ID}.json"
    assert plans.load_plan(TASK_ID, IndicatorPlan, settings, prefix="indicators_plan") == plan
    assert planner.load_plan(TASK_ID, settings) == plan
    assert planner.load_plan("0192f000-0000-7000-8000-0000000000ff", settings) is None


def test_adj_close_without_column_is_validation_error(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    binance = hive_dataset()
    with pytest.raises(IndicatorValidationError, match="adj_close"):
        planner.plan_items(
            _request([binance], IndicatorSpec(name="sma", params={"source": "adj_close"})),
            settings,
            task_id=TASK_ID,
        )


def test_adj_close_on_yfinance_is_planned(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    aapl = hive_dataset(exchange="yfinance", market_type="equity", symbol="AAPL", timeframe="1d")
    plan = planner.plan_items(
        _request([aapl], IndicatorSpec(name="sma", params={"period": 50, "source": "adj_close"})),
        settings,
        task_id=TASK_ID,
    )
    assert [i.indicator_id for i in plan.items] == ["sma_50_adj_close"]


def test_invalid_parameter_is_validation_error(
    hive_dataset: DatasetFactory, settings: FinancesSettings
) -> None:
    ds = hive_dataset()
    with pytest.raises(IndicatorValidationError, match="period"):
        planner.plan_items(
            _request([ds], IndicatorSpec(name="rsi", params={"period": 1})),
            settings,
            task_id=TASK_ID,
        )


def test_missing_dataset_is_not_found(settings: FinancesSettings) -> None:
    ghost = DatasetIdentifier.parse("binance/spot/NOPE/1h")
    with pytest.raises(SourceNotFoundError):
        planner.plan_items(_request([ghost], IndicatorSpec(name="rsi")), settings, task_id=TASK_ID)


def test_planning_writes_nothing(hive_dataset: DatasetFactory, settings: FinancesSettings) -> None:
    ds = hive_dataset()
    planner.plan_items(_request([ds], IndicatorSpec(name="rsi")), settings, task_id=TASK_ID)
    assert not settings.INDICATORS_DIR.exists()
    assert not (settings.SHARE_DIR / "plans").exists()
