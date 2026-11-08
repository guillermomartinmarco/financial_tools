"""B1.2: contrato ``BaseStrategy``/``Signals``/``MetricsTarget`` y ``signals.finalize`` (RF-16)."""

from __future__ import annotations

from typing import ClassVar

import polars as pl
import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from finances_core.backtest.errors import InvalidSignalsError
from finances_core.backtest.strategies import (
    BaseStrategy,
    MetricsTarget,
    Signals,
    StrategyDefinitionError,
    validate_strategy_class,
)
from finances_core.backtest.strategies.signals import finalize


def _pos(values: list[int | None]) -> pl.Series:
    return pl.Series(values, dtype=pl.Int8)


# ------------------------------------------------------------------------ finalize
def test_finalize_rejects_non_int8_dtype() -> None:
    sig = pl.Series([1, 0, 1], dtype=pl.Int32)
    with pytest.raises(InvalidSignalsError, match="Int8"):
        finalize(sig, n=3, warmup=0)


def test_finalize_rejects_non_series() -> None:
    sig = [1, 0, 1]  # type: ignore[arg-type]
    with pytest.raises(InvalidSignalsError, match="pl.Series"):
        finalize(sig, n=3, warmup=0)


def test_finalize_rejects_length_mismatch() -> None:
    with pytest.raises(InvalidSignalsError, match="longitud"):
        finalize(_pos([0, 0, 0]), n=4, warmup=0)


def test_finalize_rejects_nulls_after_warmup() -> None:
    with pytest.raises(InvalidSignalsError, match="nulos"):
        finalize(_pos([0, 0, None]), n=3, warmup=2)


def test_finalize_converts_nulls_and_warmup_to_zero() -> None:
    out = finalize(_pos([None, 1, 1, 0]), n=4, warmup=2)
    assert out.to_list() == [0, 0, 1, 0]
    assert out.dtype == pl.Int8
    assert out.null_count() == 0


def test_finalize_cancels_simultaneous_entry_and_exit() -> None:
    # In the new contract, this is handled by position_from_events.
    # finalize only validates the resulting position.
    # We test that it accepts a valid position.
    out = finalize(_pos([0, 1, 0]), n=3, warmup=0)
    assert out.to_list() == [0, 1, 0]


def test_finalize_warmup_beyond_length_masks_everything() -> None:
    out = finalize(_pos([1, 1]), n=2, warmup=10)
    assert out.to_list() == [0, 0]


def test_finalize_names_output_series() -> None:
    out = finalize(_pos([0]), n=1, warmup=0)
    assert out.name == "position"


# --------------------------------------------------------------------- BaseStrategy
class DemoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: int = Field(default=2, ge=2)


class Demo(BaseStrategy[DemoParams]):
    kind = "demo_strategy"
    version = "1.2.3"
    description = "Estrategia de demostración."
    Params = DemoParams
    default_params: ClassVar[dict[str, int]] = {"window": 2}

    @staticmethod
    def warmup(params: DemoParams) -> int:
        return params.window

    @staticmethod
    def signals(candles: pl.DataFrame, params: DemoParams) -> pl.Series:
        close = candles["close"]
        # Return a position series
        return pl.Series(
            (close > close.shift(params.window)).cast(pl.Int8), 
            dtype=pl.Int8
        )


def test_valid_subclass_validates_and_runs_with_generic_params() -> None:
    validate_strategy_class(Demo)
    params = DemoParams.model_validate(Demo.default_params)
    candles = pl.DataFrame({"close": pl.Series([1.0, 2.0, 3.0, 2.0], dtype=pl.Float32)})
    out = finalize(Demo.signals(candles, params), n=4, warmup=Demo.warmup(params))
    assert out.to_list() == [0, 0, 1, 0]
    assert Demo.metrics_target is None
    assert isinstance(Demo(), Demo)


def test_subclass_without_signals_cannot_be_instantiated_or_registered() -> None:
    class NoSignals(BaseStrategy[DemoParams]):
        kind = "no_signals"
        version = "1.0.0"
        description = "x"
        Params = DemoParams
        default_params: ClassVar[dict[str, int]] = {}

        @staticmethod
        def warmup(params: DemoParams) -> int:
            return 0

    with pytest.raises(TypeError):
        NoSignals()  # type: ignore[abstract]
    with pytest.raises(StrategyDefinitionError, match="signals"):
        validate_strategy_class(NoSignals)


def test_subclass_without_warmup_cannot_be_instantiated_or_registered() -> None:
    class NoWarmup(BaseStrategy[DemoParams]):
        kind = "no_warmup"
        version = "1.0.0"
        description = "x"
        Params = DemoParams
        default_params: ClassVar[dict[str, int]] = {}

        @staticmethod
        def signals(candles: pl.DataFrame, params: DemoParams) -> Signals:
            raise NotImplementedError

    with pytest.raises(TypeError):
        NoWarmup()  # type: ignore[abstract]
    with pytest.raises(StrategyDefinitionError, match="warmup"):
        validate_strategy_class(NoWarmup)


def test_subclass_without_params_cannot_be_instantiated_or_registered() -> None:
    class NoParams(BaseStrategy[DemoParams]):
        kind = "no_params"
        version = "1.0.0"
        description = "x"
        default_params: ClassVar[dict[str, int]] = {}

        @staticmethod
        def warmup(params: DemoParams) -> int:
            return 0

        @staticmethod
        def signals(candles: pl.DataFrame, params: DemoParams) -> Signals:
            raise NotImplementedError

    with pytest.raises(TypeError, match="Params"):
        NoParams()
    with pytest.raises(StrategyDefinitionError, match="Params"):
        validate_strategy_class(NoParams)


class LooseParams(BaseModel):
    window: int = 2


@pytest.mark.parametrize(
    ("attr", "value", "match"),
    [
        ("kind", "Bad-Kind", "kind"),
        ("kind", "ab", "kind"),
        ("version", "1.0", "version"),
        ("description", "", "description"),
        ("description", "dos\nlíneas", "description"),
        ("Params", LooseParams, "extra"),
        ("Params", dict, "Params"),
        ("default_params", {"window": 1}, "default_params"),
        ("default_params", {"other": 3}, "default_params"),
        ("metrics_target", "sharpe", "metrics_target"),
    ],
)
def test_validate_rejects_bad_class_attributes(attr: str, value: object, match: str) -> None:
    broken = type("Broken", (Demo,), {attr: value})
    with pytest.raises(StrategyDefinitionError, match=match):
        validate_strategy_class(broken)


def test_validate_rejects_non_strategy_class() -> None:
    with pytest.raises(StrategyDefinitionError, match="BaseStrategy"):
        validate_strategy_class(DemoParams)


# -------------------------------------------------------------------- MetricsTarget
@pytest.mark.parametrize(
    "metric",
    ["total_return", "cagr", "max_drawdown", "sharpe", "sortino", "win_rate", "exposure"],
)
def test_metrics_target_accepts_rf7_metrics(metric: str) -> None:
    assert MetricsTarget.model_validate({"metric": metric, "direction": "max"}).metric == metric


@pytest.mark.parametrize("metric", ["profit", "calmar", "n_bars"])
def test_metrics_target_rejects_metric_outside_rf7(metric: str) -> None:
    with pytest.raises(ValidationError, match="metric"):
        MetricsTarget.model_validate({"metric": metric, "direction": "max"})


def test_metrics_target_rejects_bad_direction_and_extra_keys() -> None:
    with pytest.raises(ValidationError, match="direction"):
        MetricsTarget.model_validate({"metric": "sharpe", "direction": "up"})
    with pytest.raises(ValidationError, match="weight"):
        MetricsTarget.model_validate({"metric": "sharpe", "direction": "max", "weight": 1})


def test_strategy_with_metrics_target_validates() -> None:
    target = MetricsTarget(metric="sharpe", direction="max")
    with_target = type("WithTarget", (Demo,), {"metrics_target": target})
    validate_strategy_class(with_target)
