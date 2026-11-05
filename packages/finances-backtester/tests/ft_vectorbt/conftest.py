"""Utilidades compartidas por los tests de ``ft_vectorbt``.

Fase 1 (motor): velas canónicas (``Int64``/``Float32``) y ``Signals`` (``Boolean``)
pequeñas para los casos calculados a mano, y los tres puntos públicos de ``engine.py``
(``to_framework_input`` → ``simulate`` → ``from_framework_output``) encadenados como hace
``VectorbtEngine.run``.

Fase 2 (servicio): ``FT_PROJECT_ROOT`` temporal con el *fixture* de la suite de contrato
como fuente Hive, estrategias en una ``strategies.db`` temporal (API pública de
``ft_strategy_manager``) y lectura de ``tasks.db``. Nada se escribe fuera de ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from finances_core.backtest.engine_api import CostModel, EngineOutput, Signals
from finances_core.config import FinancesSettings
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.backtest import BacktestRequest
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.market_data import OHLCV_SCHEMA
from finances_core.schemas.strategy import StrategyCreate
from finances_core.tasks import SQLTaskManager
from ft_strategy_manager import service as strategy_service

from finances_backtester.ft_vectorbt.engine import (
    from_framework_output,
    simulate,
    to_framework_input,
)
from tests.contract.fixture import GOLDEN_COSTS, GOLDEN_PARAMS, TIMEFRAME, fixture_candles

#: Inicio de las velas sintéticas (ms UTC) y paso de una hora.
T0_MS = 1_700_000_000_000
STEP_MS = 3_600_000
NO_COSTS = CostModel(fees_bps=0.0, slippage_bps=0.0)


def times(n: int) -> list[int]:
    """``open_time_ms`` de ``n`` barras horarias consecutivas."""
    return [T0_MS + i * STEP_MS for i in range(n)]


def make_candles(
    open_: Sequence[float],
    close: Sequence[float],
    open_time_ms: Sequence[int] | None = None,
) -> pl.DataFrame:
    """Velas RF-4 canónicas; ``high``/``low`` envuelven ``open``/``close``."""
    n = len(open_)
    stamps = list(open_time_ms) if open_time_ms is not None else times(n)
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series(stamps, dtype=pl.Int64),
            "open": pl.Series(list(open_), dtype=pl.Float32),
            "high": pl.Series([max(o, c) for o, c in zip(open_, close, strict=True)]).cast(
                pl.Float32
            ),
            "low": pl.Series([min(o, c) for o, c in zip(open_, close, strict=True)]).cast(
                pl.Float32
            ),
            "close": pl.Series(list(close), dtype=pl.Float32),
        }
    )


def make_signals(n: int, entries: Sequence[int] = (), exits: Sequence[int] = ()) -> Signals:
    """``Signals`` con ``True`` en las barras indicadas (señal al cierre de ``t``)."""
    return Signals(
        entries=pl.Series("entries", [i in entries for i in range(n)], dtype=pl.Boolean),
        exits=pl.Series("exits", [i in exits for i in range(n)], dtype=pl.Boolean),
    )


def run_pipeline(
    candles: pl.DataFrame,
    signals: Signals,
    costs: CostModel = NO_COSTS,
    size_fraction: float = 1.0,
    initial_cash: float = 1000.0,
) -> EngineOutput:
    """Los tres puntos públicos de ``engine.py`` encadenados (igual que ``run``)."""
    inp = to_framework_input(candles, signals)
    res = simulate(inp, costs, size_fraction, initial_cash)
    return from_framework_output(res, candles["open_time_ms"])


# ------------------------------------------------------------------ servicio (Fase 2)
#: Dataset Hive del *fixture* de contrato bajo ``FT_PARQUET_DIR`` temporal.
DATASET = DatasetIdentifier(
    exchange="binance", market_type="spot", symbol="BTCUSDT", timeframe=TIMEFRAME
)


def write_fixture_dataset(parquet_dir: Path, ds: DatasetIdentifier = DATASET) -> int:
    """Escribe el *fixture* determinista de la suite de contrato como fuente Hive canónica.

    Returns:
        Número de velas escritas.
    """
    frame = fixture_candles().to_frame()
    n = frame.height
    volume = pl.Series([10.0 + i for i in range(n)], dtype=pl.Float32)
    df = frame.with_columns(
        pl.lit(ds.symbol).alias("symbol"),
        pl.lit(ds.timeframe).alias("timeframe"),
        pl.lit(ds.market_type).alias("market_type"),
        volume.alias("volume"),
        volume.alias("quote_volume"),
        pl.lit(3, dtype=pl.Int64).alias("trades_count"),
        volume.alias("taker_buy_base_volume"),
        volume.alias("taker_buy_quote_volume"),
    )
    dt = pl.from_epoch(pl.col("open_time_ms"), time_unit="ms")
    df = df.select([pl.col(c).cast(t) for c, t in OHLCV_SCHEMA.items()]).with_columns(
        dt.dt.year().alias("year"), dt.dt.month().alias("month")
    )
    write_parquet_partitioned(df, parquet_dir, exchange=ds.exchange, schema=OHLCV_SCHEMA)
    return n


@pytest.fixture
def service_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FinancesSettings:
    """``FT_PROJECT_ROOT`` temporal (parquet, resultados, ``tasks.db``, ``strategies.db``).

    ``FT_STRATEGIES_DIR`` apunta a ``tmp_path/strategies`` (vacío salvo que el test cree
    una estrategia local) y la fuente es el *fixture* de contrato (2 400 velas ``1h``).
    """
    monkeypatch.setenv("FT_PROJECT_ROOT", str(tmp_path))
    for name in (
        "FT_PARQUET_DIR",
        "FT_RESULTS_DIR",
        "FT_TASKS_DB_PATH",
        "FT_STRATEGIES_DB_PATH",
        "FT_SHARE_DIR",
        "FT_BACKTEST_MAX_CONCURRENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FT_STRATEGIES_DIR", str(tmp_path / "strategies"))
    cfg = FinancesSettings()
    write_fixture_dataset(cfg.PARQUET_DIR)
    return cfg


def create_strategy(parameters: dict[str, Any], name: str | None = None) -> str:
    """Crea una estrategia en la ``strategies.db`` temporal y devuelve su ``strategy_id``."""
    created = strategy_service.create_strategy(
        StrategyCreate(name=name or f"vbt-{uuid.uuid4().hex[:8]}", parameters=parameters)
    )
    return created.strategy_id


def make_request(strategy_id: str, **overrides: Any) -> BacktestRequest:
    """``BacktestRequest`` sobre :data:`DATASET` con los costes de los *golden*."""
    body: dict[str, Any] = {
        "dataset": DATASET.model_dump(),
        "strategy_id": strategy_id,
        "fees_bps": GOLDEN_COSTS.fees_bps,
        "slippage_bps": GOLDEN_COSTS.slippage_bps,
        **overrides,
    }
    return BacktestRequest.model_validate(body)


def sma_request(**overrides: Any) -> BacktestRequest:
    """Petición ``sma_cross`` con los parámetros de los *golden* (55 operaciones)."""
    params = GOLDEN_PARAMS["sma_cross"]
    return make_request(create_strategy({"kind": "sma_cross", "params": params}), **overrides)


def tree_digest(root: Path) -> dict[str, str]:
    """``{ruta relativa: sha256}`` de todos los archivos bajo ``root``."""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def task_row(settings: FinancesSettings, task_id: str) -> dict[str, Any]:
    """Fila de ``tasks.db`` (falla si no existe)."""
    row = SQLTaskManager(settings.TASKS_DB_PATH).get_task(task_id)
    assert row is not None, task_id
    return row
