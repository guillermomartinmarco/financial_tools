"""Utilidades de tests del núcleo de backtesting (``finances_core.backtest``).

Construyen carpetas de estrategia temporales (``logic.py`` + ``README.md``) para los
tests de verificación estática, descubrimiento, ``strategy_source`` y verificación de
autor. Nunca escriben fuera del directorio que recibe el test (``tmp_path``).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from finances_core.backtest.catalog import get_registry
from finances_core.backtest.strategies.signals import finalize
from finances_core.config import FinancesSettings

#: ``logic.py`` válido: momento simple sobre ``window`` barras. ``{kind}`` se sustituye.
VALID_LOGIC = '''\
"""Estrategia de prueba: momento sobre ``window`` barras."""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from finances_core.backtest.strategies import BaseStrategy
from finances_core.backtest.strategies._lib import position_from_events


class MomentumParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: int = Field(default=3, ge=2, le=100)


class Momentum(BaseStrategy[MomentumParams]):
    kind = "{kind}"
    version = "1.0.0"
    description = "Momento de prueba sobre window barras."
    Params = MomentumParams
    default_params = {{"window": 3}}

    @staticmethod
    def warmup(params: MomentumParams) -> int:
        return params.window

    @staticmethod
    def signals(candles: pl.DataFrame, params: MomentumParams) -> pl.Series:
        close = candles["close"]
        past = close.shift(params.window)
        return pl.select(position_from_events(close > past, close < past)).to_series()
'''

#: README válido para :data:`VALID_LOGIC`. ``{table}`` es la tabla de parámetros.
VALID_TABLE = (
    "| Parámetro | Tipo | Rango | Defecto | Descripción |\n"
    "|---|---|---|---|---|\n"
    "| `window` | entero | 2..100 | 3 | Barras hacia atrás |\n"
)

README_TEMPLATE = """\
# Estrategia de prueba

## Lógica de negocio

Compra cuando el cierre supera el de hace `window` barras.

## Matemática

`entry[t] = close[t] > close[t-window]`; sin señal durante el calentamiento.

## Diagrama

```mermaid
stateDiagram-v2
    Plano --> Largo: entry
    Largo --> Plano: exit
```

## Diccionario de parámetros

{table}
## Limitaciones

Solo de ejemplo.
"""

VALID_README = README_TEMPLATE.format(table=VALID_TABLE)


def write_strategy(  # noqa: PLR0913 - helper de tests con opciones nombradas
    root: Path,
    folder: str,
    *,
    kind: str | None = None,
    logic: str | None = VALID_LOGIC,
    readme: str | None = VALID_README,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Crea ``root/folder`` con ``logic.py`` y ``README.md`` (``None`` omite el archivo).

    ``kind`` (por defecto, el nombre de la carpeta) se inserta en :data:`VALID_LOGIC` o en
    cualquier ``logic`` que contenga ``{kind}``; ``extra_files`` añade módulos hermanos.
    """
    target = root / folder
    target.mkdir(parents=True, exist_ok=True)
    if logic is not None:
        text = logic.format(kind=kind or folder) if "{kind}" in logic else logic
        (target / "logic.py").write_text(text, encoding="utf-8")
    if readme is not None:
        (target / "README.md").write_text(readme, encoding="utf-8")
    for name, content in (extra_files or {}).items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return target


def candles_frame(closes: list[float]) -> pl.DataFrame:
    """Velas RF-4 (``Int64`` / ``Float32``) con ``open = high = low = close``."""
    return ohlc_frame(high=closes, low=closes, close=closes)


def ohlc_frame(*, high: list[float], low: list[float], close: list[float]) -> pl.DataFrame:
    """Velas RF-4 (``Int64`` / ``Float32``) con ``open = close`` y ``high``/``low`` propios."""
    n = len(close)
    return pl.DataFrame(
        {
            "open_time_ms": pl.Series([i * 3_600_000 for i in range(n)], dtype=pl.Int64),
            "open": pl.Series(close, dtype=pl.Float32),
            "high": pl.Series(high, dtype=pl.Float32),
            "low": pl.Series(low, dtype=pl.Float32),
            "close": pl.Series(close, dtype=pl.Float32),
        }
    )


def builtin_position(
    kind: str, candles: pl.DataFrame, **params: int | float
) -> list[int]:
    """Posición-objetivo **normalizada** (``finalize``) de la integrada ``kind`` como lista.

    Usa el registro real del proceso (sin ``FT_STRATEGIES_DIR``), igual que el ``runner``.
    Los valores son ``Int8`` en ``{-1, 0, +1}`` (hoy solo largo/plano: ``{0, +1}``).
    """
    strategy = get_registry(FinancesSettings()).get(kind)
    validated = strategy.Params.model_validate(params)
    raw = strategy.signals(candles, validated)
    pos = finalize(raw, n=candles.height, warmup=strategy.warmup(validated))
    return pos.to_list()
