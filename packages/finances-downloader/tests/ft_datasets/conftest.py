"""Fixtures de ``ft_datasets``: recetas temporales y :class:`FakeRunner` (sin red ni subprocesos).

El *runner* falso imita a la CLI de los descargadores: interpreta el ``argv`` exacto que
construye ``ft_datasets`` y escribe velas canónicas sintéticas en el ``FT_PARQUET_DIR``
del **entorno recibido** (así se comprueba que el hijo escribe donde el padre lee).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from finances_core.config import FinancesSettings
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.schemas.datasets import BlockRun, RecipeItem
from finances_core.schemas.market_data import OHLCV_SCHEMA, TF_MS_MAP
from finances_core.schemas.market_data_yfinance import YF_OHLCV_SCHEMA

RECIPE_NAME = "mini-v1"
#: Receta pequeña: 2 bloques, 3 ítems (binance 2 × 1h, yfinance 1 × 1d).
RECIPE_TOML = """\
schema_version = 1
name = "mini-v1"
description = "Receta de tests"

[[datasets]]
exchange = "binance"
market_type = "spot"
symbols = ["BTCUSDT", "ETHUSDT"]
timeframes = ["1h"]
start = 2024-01-01
end = 2024-03-01

[[datasets]]
exchange = "yfinance"
market_type = "equity"
symbols = ["AAPL"]
timeframes = ["1d"]
start = 2024-01-01
end = 2024-02-01
"""
BTC_KEY = "binance/spot/BTCUSDT/1h@2024-01-01..2024-03-01"
ETH_KEY = "binance/spot/ETHUSDT/1h@2024-01-01..2024-03-01"
AAPL_KEY = "yfinance/equity/AAPL/1d@2024-01-01..2024-02-01"
ALL_KEYS = [BTC_KEY, ETH_KEY, AAPL_KEY]
UNKNOWN_TASK = "0192f000-0000-7000-8000-0000000000ff"


@pytest.fixture
def settings(tmp_path: Path) -> FinancesSettings:
    """Settings con ``FT_PROJECT_ROOT`` temporal (fijado por el conftest raíz)."""
    cfg = FinancesSettings()
    assert tmp_path.resolve() == cfg.PROJECT_ROOT
    return cfg


@pytest.fixture
def recipe_dir(settings: FinancesSettings) -> Path:
    """``FT_DATASETS_RECIPES_DIR`` con la receta ``mini-v1``."""
    directory = settings.DATASETS_RECIPES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{RECIPE_NAME}.toml").write_text(RECIPE_TOML, encoding="utf-8")
    return directory


def candles(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    *,
    scale: float = 1.0,
) -> pl.DataFrame:
    """Velas canónicas deterministas en ``[start_ms, end_ms)`` (``yfinance`` 1d = 24/7)."""
    times = np.arange(start_ms, end_ms, TF_MS_MAP[timeframe], dtype=np.int64)
    n = times.size
    seed = sum(map(ord, symbol))
    base = ((seed + np.arange(n, dtype=np.float64) * 0.5) * scale).astype(np.float32)
    data: dict[str, object] = {
        "symbol": [symbol] * n,
        "timeframe": [timeframe] * n,
        "market_type": [market_type] * n,
        "open_time_ms": times,
        "open": base,
        "high": base + np.float32(1.0),
        "low": base - np.float32(1.0),
        "close": base + np.float32(0.25),
        "volume": np.full(n, 10.0, dtype=np.float32),
    }
    if exchange == "binance":
        data |= {
            "quote_volume": np.full(n, 1000.0, dtype=np.float32),
            "trades_count": np.arange(n, dtype=np.int64),
            "taker_buy_base_volume": np.full(n, 5.0, dtype=np.float32),
            "taker_buy_quote_volume": np.full(n, 500.0, dtype=np.float32),
        }
        schema = OHLCV_SCHEMA
    else:
        data["adj_close"] = base
        schema = YF_OHLCV_SCHEMA
    return pl.DataFrame(data).select([pl.col(c).cast(t) for c, t in schema.items()])


def write_frame(parquet_dir: Path, exchange: str, frame: pl.DataFrame) -> None:
    """Escribe ``frame`` con el escritor Hive de los descargadores (``replace`` por mes)."""
    schema = OHLCV_SCHEMA if exchange == "binance" else YF_OHLCV_SCHEMA
    dt = pl.from_epoch("open_time_ms", time_unit="ms")
    keyed = frame.with_columns(
        dt.dt.year().cast(pl.Int32).alias("year"), dt.dt.month().cast(pl.Int32).alias("month")
    )
    write_parquet_partitioned(
        keyed,
        parquet_dir,
        exchange=exchange,
        schema={**schema, "year": pl.Int32, "month": pl.Int32},
        mode="replace",
    )


def write_item(parquet_dir: Path, item: RecipeItem, *, scale: float = 1.0) -> None:
    """Almacén completo y canónico de ``item``."""
    frame = candles(
        item.exchange,
        item.market_type,
        item.symbol,
        item.timeframe,
        item.start_ms,
        item.end_ms,
        scale=scale,
    )
    write_frame(parquet_dir, item.exchange, frame)


def _values(argv: Sequence[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


@dataclass
class FakeRunner:
    """``Runner`` falso: escribe velas sintéticas en ``env["FT_PARQUET_DIR"]``."""

    exit_code: int = 0
    timed_out: bool = False
    no_data_symbols: set[str] = field(default_factory=set)
    stderr: list[str] = field(default_factory=lambda: ['{"event": "x", "task_id": null}'])
    calls: list[tuple[int, list[str], dict[str, str], float]] = field(default_factory=list)
    on_call: Callable[[list[str]], None] | None = None

    def run_block(
        self, block: int, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> BlockRun:
        args = list(argv)
        self.calls.append((block, args, dict(env), timeout_s))
        if self.on_call is not None:
            self.on_call(args)
        if self.exit_code == 0 and not self.timed_out:
            exchange = "binance" if args[0] == "ft_market_data_binance" else "yfinance"
            market = _values(args, "--market")[0] if exchange == "binance" else "equity"
            start, end = int(_values(args, "--start")[0]), int(_values(args, "--end")[0]) + 1
            for symbol in _values(args, "--symbol"):
                if symbol in self.no_data_symbols:
                    continue
                for timeframe in _values(args, "--timeframe"):
                    frame = candles(exchange, market, symbol, timeframe, start, end)
                    write_frame(Path(env["FT_PARQUET_DIR"]), exchange, frame)
        return BlockRun(
            block=block,
            exit_code=None if self.timed_out else self.exit_code,
            duration_ms=1,
            timed_out=self.timed_out,
            stderr_tail=list(self.stderr),
            downloader_task_id=None,
        )
