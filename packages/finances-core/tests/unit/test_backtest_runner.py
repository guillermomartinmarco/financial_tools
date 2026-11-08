"""B5.1: orquestación ``finances_core.backtest.runner.run_backtest`` (RF-3..RF-10, RF-13).

Motor *fake* determinista (simulación larga/plana de RF-5 con bucle explícito) sobre una
fuente Hive sintética bajo ``FT_PARQUET_DIR`` temporal; la estrategia vive en una
``strategies.db`` temporal (``FT_PROJECT_ROOT`` del conftest) y, para las locales, en
``tmp_path``. Nada se escribe fuera de ``tmp_path``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from backtest_helpers import VALID_LOGIC, write_strategy
from indicators_helpers import BINANCE, write_source
from structlog.testing import capture_logs

from finances_core.backtest import identity, metrics, results_store, runner
from finances_core.backtest.candles import CANDLE_COLUMNS
from finances_core.backtest.catalog import (
    BUILTIN_DIR,
    StrategyRegistry,
    build_registry,
    get_registry,
)
from finances_core.backtest.costs import buy_price, fee_rate, sell_price
from finances_core.backtest.engine_api import EngineOutput
from finances_core.backtest.errors import (
    InvalidSignalsError,
    OutputOutOfRangeError,
    StrategyInitError,
    StrategyRuntimeError,
)
from finances_core.backtest.frames import (
    EQUITY_SCHEMA,
    EXIT_REASON_END_OF_DATA,
    EXIT_REASON_SIGNAL,
    SIDE_LONG,
    TRADES_SCHEMA,
)
from finances_core.backtest.strategies import Signals
from finances_core.backtest.strategies.signals import finalize, position_to_signals
from finances_core.config import FinancesSettings
from finances_core.marketdata.tasks_lifecycle import json_field
from finances_core.schemas.backtest import BacktestRequest, CostModel
from finances_core.schemas.strategy import StrategyCreate
from finances_core.tasks import SQLTaskManager
from ft_strategy_manager import service

TASK_ID = "01890a5d-ac96-774b-bcce-b302099a8001"
FULL_PHASES = ["LOADING", "SIGNALS", "SIMULATING", "PERSISTING", "COMPLETED"]
AUDIT_FIELDS = {
    "task_id",
    "run_id",
    "engine",
    "engine_version",
    "strategy_id",
    "strategy_version",
    "strategy_hash",
    "kind",
    "kind_version",
    "strategy_code_hash",
    "strategy_origin",
    "dataset",
    "start_ms",
    "end_ms",
    "n_bars",
    "n_trades",
    "cached",
    "forced",
    "duration_ms",
    "result",
    "load_ms",
    "signals_ms",
    "engine_ms",
    "persist_ms",
}


# ------------------------------------------------------------------ motor fake
class FakeEngine:
    """Simulación determinista de RF-5: señal al cierre de ``t`` → apertura de ``t+1``."""

    name = "fake"

    def __init__(self, *, corrupt: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.corrupt = corrupt

    def version(self) -> str:
        return "fake 1.0.0"

    def run(
        self,
        candles: pl.DataFrame,
        signals: Signals,
        costs: CostModel,
        size_fraction: float,
        initial_cash: float,
    ) -> EngineOutput:
        self.calls.append(
            {
                "candles": candles,
                "signals": signals,
                "costs": costs,
                "size_fraction": size_fraction,
                "initial_cash": initial_cash,
            }
        )
        out = _simulate(candles, signals, costs, size_fraction, initial_cash)
        if self.corrupt:  # dtype erróneo en una tabla canónica
            out = EngineOutput(
                trades=out.trades,
                equity=out.equity.with_columns(pl.col("equity").cast(pl.Int64)),
            )
        return out


def _simulate(  # noqa: PLR0914 - bucle explícito de referencia, solo en tests
    candles: pl.DataFrame,
    signals: Signals,
    costs: CostModel,
    size_fraction: float,
    initial_cash: float,
) -> EngineOutput:
    times = candles["open_time_ms"].to_list()
    opens = candles["open"].to_list()
    closes = candles["close"].to_list()
    entries = signals.entries.to_list()
    exits = signals.exits.to_list()
    rate = fee_rate(costs)
    n = len(times)
    cash = initial_cash
    size = 0.0
    entry: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    equity: list[float] = []

    def close_position(t: int, price: float, reason: int) -> None:
        nonlocal cash, size
        exit_fee = size * price * rate
        cash += size * price - exit_fee
        pnl = size * (price - entry["price"]) - entry["fee"] - exit_fee
        rows.append(
            {
                "entry_time_ms": int(entry["time"]),
                "exit_time_ms": times[t],
                "side": SIDE_LONG,
                "entry_price": entry["price"],
                "exit_price": price,
                "size": size,
                "fees": entry["fee"] + exit_fee,
                "pnl": pnl,
                "return": pnl / entry["cost"],
                "exit_reason": reason,
            }
        )
        size = 0.0

    for t in range(n):
        # RF-5 rule 1: an entry that would execute on the last bar is ignored.
        if 0 < t < n - 1 and size == 0.0 and entries[t - 1]:
            price = float(buy_price(opens[t], costs))
            budget = cash * size_fraction
            size = budget / (price * (1 + rate))
            fee = size * price * rate
            cash -= size * price + fee
            entry = {"time": times[t], "price": price, "fee": fee, "cost": size * price + fee}
        elif t > 0 and size > 0.0 and exits[t - 1]:
            close_position(t, float(sell_price(opens[t], costs)), EXIT_REASON_SIGNAL)
        if t == n - 1 and size > 0.0:
            close_position(t, float(sell_price(closes[t], costs)), EXIT_REASON_END_OF_DATA)
        equity.append(cash + size * closes[t])

    trades = (
        pl.DataFrame(rows, schema=TRADES_SCHEMA) if rows else pl.DataFrame(schema=TRADES_SCHEMA)
    )
    eq = pl.DataFrame(
        {
            "open_time_ms": pl.Series(times, dtype=pl.Int64),
            "equity": pl.Series(equity, dtype=pl.Float32),
        }
    )
    eq = eq.with_columns(
        (pl.col("equity") / pl.col("equity").cum_max() - 1).clip(upper_bound=0).alias("drawdown")
    ).select([pl.col(c).cast(t) for c, t in EQUITY_SCHEMA.items()])
    return EngineOutput(trades=trades, equity=eq, warnings=["fake_warning"])


# -------------------------------------------------------------------- fixtures
@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FinancesSettings:
    monkeypatch.setenv("FT_STRATEGIES_DIR", str(tmp_path / "no_local_strategies"))
    cfg = FinancesSettings()
    write_source(cfg.PARQUET_DIR, BINANCE, rows=120)
    return cfg


@pytest.fixture
def registry(settings: FinancesSettings) -> StrategyRegistry:
    return get_registry(settings)


@pytest.fixture
def signal_calls(registry: StrategyRegistry, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Espía de ``sma_cross.signals``: cuántas veces calcula señales el ``runner``."""
    strategy = registry.get("sma_cross")
    original = strategy.signals
    calls: list[int] = []

    def spy(candles: pl.DataFrame, params: Any) -> Signals:
        calls.append(candles.height)
        return original(candles, params)

    monkeypatch.setattr(strategy, "signals", staticmethod(spy))
    return calls


def _create(parameters: dict[str, Any]) -> str:
    created = service.create_strategy(StrategyCreate(name="runner", parameters=parameters))
    return created.strategy_id


def _request(strategy_id: str, **overrides: Any) -> BacktestRequest:
    body: dict[str, Any] = {
        "dataset": BINANCE.model_dump(),
        "strategy_id": strategy_id,
        "fees_bps": 10.0,
        "slippage_bps": 5.0,
        **overrides,
    }
    return BacktestRequest.model_validate(body)


def _sma_request(**overrides: Any) -> BacktestRequest:
    return _request(_create({"kind": "sma_cross", "params": {"fast": 3, "slow": 7}}), **overrides)


def _task(settings: FinancesSettings, task_id: str = TASK_ID) -> dict[str, Any]:
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(task_id)
    assert row is not None
    return row


def _files(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _phases() -> tuple[list[str], Callable[[str], None]]:
    seen: list[str] = []
    return seen, seen.append


def _local_registry(local: Path) -> StrategyRegistry:
    return build_registry(builtin_dir=BUILTIN_DIR, local_dir=local)


# ----------------------------------------------------------------------- tests
def test_full_pipeline_writes_run_and_completes_task(
    settings: FinancesSettings, registry: StrategyRegistry, signal_calls: list[int]
) -> None:
    engine = FakeEngine()
    request = _sma_request()
    seen, on_phase = _phases()

    result = runner.run_backtest(
        request, engine, settings, on_phase, task_id=TASK_ID, registry=registry
    )

    assert result.cached is False
    assert seen == FULL_PHASES
    assert signal_calls == [120]
    # El motor recibe las velas de RF-4 y señales ya normalizadas por el núcleo.
    (call,) = engine.calls
    assert call["candles"].columns == list(CANDLE_COLUMNS)
    assert call["candles"].height == 120
    assert call["costs"] == CostModel(fees_bps=10.0, slippage_bps=5.0)
    assert (call["size_fraction"], call["initial_cash"]) == (1.0, 10_000.0)
    strategy = registry.get("sma_cross")
    params = strategy.Params.model_validate({"fast": 3, "slow": 7})
    expected = position_to_signals(
        finalize(strategy.signals(call["candles"], params), n=120, warmup=strategy.warmup(params))
    )
    assert call["signals"].entries.to_list() == expected.entries.to_list()
    assert call["signals"].exits.to_list() == expected.exits.to_list()
    assert call["signals"].entries.any()

    # run_id solo vía identity, con el rango efectivo y los datos cargados.
    manifest = result.manifest
    times = call["candles"]["open_time_ms"]
    assert result.run_id == identity.run_id(
        request,
        manifest.strategy,
        start_ms=times[0],
        end_ms=times[-1],
        data_hash=identity.data_hash(call["candles"]),
        engine="fake",
        engine_version="fake 1.0.0",
    )
    assert manifest.run_id == result.run_id
    assert (manifest.engine, manifest.engine_version) == ("fake", "fake 1.0.0")
    assert (manifest.start_ms, manifest.end_ms, manifest.n_bars) == (times[0], times[-1], 120)
    assert manifest.data_hash == identity.data_hash(call["candles"])
    assert manifest.request == request
    assert (manifest.strategy.kind, manifest.strategy.origin) == ("sma_cross", "builtin")
    assert manifest.kind_version == strategy.version
    assert "fake_warning" in manifest.warnings

    # Resumen = metrics.compute sobre la salida validada del motor.
    out = _simulate(call["candles"], call["signals"], call["costs"], 1.0, 10_000.0)
    computed = metrics.compute(out.trades, out.equity, initial_cash=10_000.0, timeframe="1h")
    assert result.summary.model_dump(exclude={"start_ms", "end_ms", "warnings"}) == (
        computed.model_dump(exclude={"start_ms", "end_ms", "warnings"})
    )
    assert result.summary.n_trades > 0

    stored = results_store.find_run(settings.RESULTS_DIR, result.run_id)
    assert stored is not None
    assert stored.manifest == manifest
    assert stored.summary == result.summary
    assert pl.read_parquet(stored.path / results_store.TRADES_FILE).equals(out.trades)

    task = _task(settings)
    assert (task["status"], task["phase"]) == ("COMPLETED", "COMPLETED")
    summary = json_field(task, "result_summary_json", {})
    assert summary == {
        "run_id": result.run_id,
        "cached": False,
        "n_bars": 120,
        "n_trades": result.summary.n_trades,
    }


def test_repeat_is_cached_without_engine_or_signals(
    settings: FinancesSettings, registry: StrategyRegistry, signal_calls: list[int]
) -> None:
    engine = FakeEngine()
    request = _sma_request()
    first = runner.run_backtest(request, engine, settings, registry=registry)
    before = _files(settings.RESULTS_DIR)
    seen, on_phase = _phases()

    again = runner.run_backtest(
        request, engine, settings, on_phase, task_id=TASK_ID, registry=registry
    )

    assert again.cached is True
    assert again.run_id == first.run_id
    assert again.summary == first.summary
    assert again.manifest == first.manifest
    assert len(engine.calls) == 1  # el motor no se invoca
    assert signal_calls == [120]  # ni se calculan señales
    assert seen == ["LOADING", "COMPLETED"]
    assert _files(settings.RESULTS_DIR) == before
    task = _task(settings)
    assert task["status"] == "COMPLETED"
    assert json_field(task, "result_summary_json", {})["cached"] is True


def test_force_recomputes_signals_and_engine(
    settings: FinancesSettings, registry: StrategyRegistry, signal_calls: list[int]
) -> None:
    engine = FakeEngine()
    request = _sma_request()
    first = runner.run_backtest(request, engine, settings, registry=registry)
    run_path = results_store.find_run(settings.RESULTS_DIR, first.run_id)
    assert run_path is not None
    data_files = (results_store.TRADES_FILE, results_store.EQUITY_FILE, "summary.json")
    before = {name: (run_path.path / name).read_bytes() for name in data_files}
    seen, on_phase = _phases()

    forced = runner.run_backtest(
        request.model_copy(update={"force": True}), engine, settings, on_phase, registry=registry
    )

    assert forced.cached is False
    assert forced.run_id == first.run_id  # force no forma parte de la identidad
    assert len(engine.calls) == 2
    assert signal_calls == [120, 120]
    assert seen == FULL_PHASES
    assert {name: (run_path.path / name).read_bytes() for name in data_files} == before
    # The stored manifest is the forced one: force replaces, it does not return the old run.
    stored = results_store.read_manifest(run_path.path)
    assert stored is not None
    assert stored.created_at_ms == forced.manifest.created_at_ms


def test_editing_local_strategy_code_changes_run_id(
    settings: FinancesSettings, tmp_path: Path
) -> None:
    local = tmp_path / "local"
    folder = write_strategy(local, "momentum_kind")
    engine = FakeEngine()
    request = _request(_create({"kind": "momentum_kind", "params": {"window": 3}}))
    first = runner.run_backtest(request, engine, settings, registry=_local_registry(local))
    assert first.manifest.strategy.origin == "local"

    logic = folder / "logic.py"
    logic.write_text(logic.read_text(encoding="utf-8") + "\n# edición\n", encoding="utf-8")
    edited = runner.run_backtest(request, engine, settings, registry=_local_registry(local))

    assert edited.run_id != first.run_id
    assert edited.cached is False
    assert edited.manifest.strategy_code_hash != first.manifest.strategy_code_hash
    assert len(engine.calls) == 2


def _assert_failed_without_writing(
    settings: FinancesSettings, engine: FakeEngine, code: str, *, engine_calls: int = 0
) -> None:
    assert len(engine.calls) == engine_calls
    assert _files(settings.RESULTS_DIR) == {}
    task = _task(settings)
    assert (task["status"], task["phase"]) == ("FAILED", "FAILED")
    assert task["error"].startswith(f"{code}: ")


@pytest.mark.parametrize(
    ("body", "error", "code"),
    [
        (
            "return pl.select(position_from_events(close > past, close < past)).to_series() * 2",
            InvalidSignalsError,
            "invalid_signals",
        ),
        ('raise ValueError("boom")', StrategyRuntimeError, "strategy_runtime_error"),
    ],
)
def test_signal_failures_fail_task_without_writing(
    settings: FinancesSettings,
    tmp_path: Path,
    body: str,
    error: type[Exception],
    code: str,
) -> None:
    local = tmp_path / "local"
    original = "return pl.select(position_from_events(close > past, close < past)).to_series()"
    assert original in VALID_LOGIC
    write_strategy(local, "bad_kind", logic=VALID_LOGIC.replace(original, body))
    engine = FakeEngine()
    request = _request(_create({"kind": "bad_kind", "params": {}}))
    seen, on_phase = _phases()

    with capture_logs() as logs, pytest.raises(error) as exc:
        runner.run_backtest(
            request, engine, settings, on_phase, task_id=TASK_ID, registry=_local_registry(local)
        )

    assert exc.value.code == code  # type: ignore[attr-defined]
    assert exc.value.exit_code == 3  # type: ignore[attr-defined]
    assert exc.value.http_status == 500  # type: ignore[attr-defined]
    assert seen == ["LOADING", "SIGNALS", "FAILED"]
    _assert_failed_without_writing(settings, engine, code)
    if error is StrategyRuntimeError:
        assert isinstance(exc.value.__cause__, ValueError)
        assert "Traceback" not in str(exc.value)
        logged = next(e for e in logs if e["event"] == "strategy_runtime_error")
        assert logged["log_level"] == "error"
        assert logged["exc_info"] is True


def test_invalid_local_strategy_is_strategy_init_error(
    settings: FinancesSettings, tmp_path: Path
) -> None:
    local = tmp_path / "local"
    write_strategy(local, "broken_kind", readme=None)
    engine = FakeEngine()
    request = _request(_create({"kind": "broken_kind", "params": {}}))

    with pytest.raises(StrategyInitError) as exc:
        runner.run_backtest(
            request, engine, settings, task_id=TASK_ID, registry=_local_registry(local)
        )

    assert (exc.value.code, exc.value.exit_code, exc.value.http_status) == (
        "strategy_init_error",
        2,
        500,
    )
    assert exc.value.origin == "local"
    _assert_failed_without_writing(settings, engine, "strategy_init_error")


def test_invalid_engine_output_fails_without_writing(
    settings: FinancesSettings, registry: StrategyRegistry
) -> None:
    engine = FakeEngine(corrupt=True)
    seen, on_phase = _phases()

    with pytest.raises(OutputOutOfRangeError) as exc:
        runner.run_backtest(
            _sma_request(), engine, settings, on_phase, task_id=TASK_ID, registry=registry
        )

    assert exc.value.code == "output_out_of_range"
    assert seen == ["LOADING", "SIGNALS", "SIMULATING", "FAILED"]
    _assert_failed_without_writing(settings, engine, "output_out_of_range", engine_calls=1)


def test_insufficient_history_warning(
    settings: FinancesSettings, registry: StrategyRegistry
) -> None:
    """RF-4: menos barras que ``warmup + 2`` → corrida válida, 0 operaciones y advertencia."""
    request = _request(_create({"kind": "sma_cross", "params": {"fast": 3, "slow": 200}}))

    result = runner.run_backtest(request, FakeEngine(), settings, registry=registry)

    assert result.summary.n_trades == 0
    assert "insufficient_history" in result.manifest.warnings
    assert "insufficient_history" in result.summary.warnings


# ------------------------------------------------------------- auditoría RF-13
def _audit(logs: list[dict[str, Any]]) -> dict[str, Any]:
    (event,) = [e for e in logs if e["event"] == "backtest_run"]
    assert event["log_level"] == "info"
    assert set(event) >= AUDIT_FIELDS
    return event


def test_audit_event_for_completed_and_cached_runs(
    settings: FinancesSettings, registry: StrategyRegistry
) -> None:
    engine = FakeEngine()
    request = _sma_request()
    with capture_logs() as logs:
        result = runner.run_backtest(request, engine, settings, task_id=TASK_ID, registry=registry)
    event = _audit(logs)
    strategy = result.manifest.strategy
    assert event["task_id"] == TASK_ID
    assert event["result"] == "completed"
    assert (event["cached"], event["forced"]) == (False, False)
    assert event["run_id"] == result.run_id
    assert (event["engine"], event["engine_version"]) == ("fake", "fake 1.0.0")
    assert (event["strategy_id"], event["strategy_version"]) == (strategy.strategy_id, 1)
    assert event["strategy_hash"] == strategy.hash
    assert (event["kind"], event["kind_version"]) == ("sma_cross", strategy.kind_version)
    assert event["strategy_code_hash"] == strategy.code_hash
    assert event["strategy_origin"] == "builtin"
    assert event["dataset"] == BINANCE.path_segment()
    assert (event["start_ms"], event["end_ms"]) == (
        result.manifest.start_ms,
        result.manifest.end_ms,
    )
    assert (event["n_bars"], event["n_trades"]) == (120, result.summary.n_trades)
    for metric in ("duration_ms", "load_ms", "signals_ms", "engine_ms", "persist_ms"):
        assert isinstance(event[metric], float)
        assert event[metric] >= 0

    with capture_logs() as logs:
        runner.run_backtest(request, engine, settings, registry=registry)
    cached = _audit(logs)
    assert (cached["result"], cached["cached"]) == ("cached", True)
    assert cached["run_id"] == result.run_id
    assert cached["task_id"] is None
    assert (cached["signals_ms"], cached["engine_ms"], cached["persist_ms"]) == (None, None, None)


def test_audit_event_for_failed_run(settings: FinancesSettings, registry: StrategyRegistry) -> None:
    with capture_logs() as logs, pytest.raises(OutputOutOfRangeError):
        runner.run_backtest(
            _sma_request(force=True),
            FakeEngine(corrupt=True),
            settings,
            task_id=TASK_ID,
            registry=registry,
        )
    event = _audit(logs)
    assert event["result"] == "failed"
    assert event["error"].startswith("output_out_of_range: ")
    assert (event["cached"], event["forced"]) == (False, True)
    assert event["kind"] == "sma_cross"
    assert event["strategy_origin"] == "builtin"
    assert event["run_id"] is not None
    assert event["persist_ms"] is None
    assert isinstance(event["signals_ms"], float)
