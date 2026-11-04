"""Lector de velas del backtesting con rango inclusivo (RF-4, plan §1).

Reutiliza el lector y la validación compartidos de :mod:`finances_core.indicators_store.source`
(``load_source``, ``validate_source``) sobre ``FT_PARQUET_DIR`` en **solo lectura**: este
módulo nunca escribe bajo ese directorio.

Validación (Art. 13): ``open_time_ms`` ``Int64`` estrictamente creciente y único, precios
``Float32`` finitos y ``> 0`` en el rango pedido; nunca se descartan filas en silencio. Los
huecos de datos no son error: se cuentan en ``data_gaps`` (intervalos ``> 1,5 × timeframe``).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
import structlog

from finances_core.backtest.errors import EmptyRangeError
from finances_core.config import FinancesSettings
from finances_core.indicators_store.source import (
    TIME_COLUMN,
    InvalidSourceError,
    dataset_dir,
    load_source,
    validate_source,
)
from finances_core.marketdata.timeframes import timeframe_duration_ms
from finances_core.schemas.backtest import BacktestDataset
from finances_core.schemas.data_viewer import DatasetIdentifier

logger = structlog.get_logger(__name__)

PRICE_COLUMNS = ("open", "high", "low", "close")
#: Columnas exactas que recibe ``Strategy.signals`` (RF-4, RF-16); sin volumen en v1.0.0.
CANDLE_COLUMNS = (TIME_COLUMN, *PRICE_COLUMNS)


@dataclass(frozen=True)
class Candles:
    """Velas validadas del rango efectivo y su conteo de huecos (RF-4)."""

    dataset: BacktestDataset
    frame: pl.DataFrame  # open_time_ms Int64 + open/high/low/close Float32
    data_gaps: int

    @property
    def n_bars(self) -> int:
        return self.frame.height

    @property
    def start_ms(self) -> int:
        """Primer ``open_time_ms`` efectivo (el frame nunca está vacío)."""
        return int(self.frame[TIME_COLUMN][0])

    @property
    def end_ms(self) -> int:
        """Último ``open_time_ms`` efectivo."""
        return int(self.frame[TIME_COLUMN][-1])


def count_data_gaps(times: pl.Series, timeframe: str) -> int:
    """Intervalos consecutivos de ``times`` estrictamente mayores que ``1,5 × timeframe``.

    Raises:
        ValueError: SI ``timeframe`` no es uno de los soportados por la suite.
    """
    duration = timeframe_duration_ms(timeframe)
    # 2·Δ > 3·duración ⇔ Δ > 1,5·duración, en enteros (sin float64, Art. 5).
    return int((times.diff().drop_nulls() * 2 > 3 * duration).sum())


def _checked_dataset(settings: FinancesSettings, dataset: DatasetIdentifier) -> BacktestDataset:
    # Revalida cada componente aunque llegue un DatasetIdentifier sin las reglas de ruta.
    ds = BacktestDataset.model_validate(dataset.model_dump())
    root = settings.PARQUET_DIR.resolve()
    target = dataset_dir(settings, ds).resolve()
    if not target.is_relative_to(root):
        msg = f"Dataset fuera de FT_PARQUET_DIR: {ds.path_segment()}"
        raise InvalidSourceError(msg)
    return ds


def _check_prices(frame: pl.DataFrame, dataset: BacktestDataset) -> None:
    for column in PRICE_COLUMNS:
        nulls = frame[column].null_count()
        if nulls:
            msg = (
                f"Columna {column!r} con {nulls} valores nulos o no finitos en "
                f"{dataset.path_segment()}"
            )
            raise InvalidSourceError(msg)
        non_positive = int((frame[column] <= 0).sum())
        if non_positive:
            msg = f"Columna {column!r} con {non_positive} precios <= 0 en {dataset.path_segment()}"
            raise InvalidSourceError(msg)


def read_candles(
    settings: FinancesSettings,
    dataset: DatasetIdentifier,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> Candles:
    """Carga las velas de ``dataset`` en ``[start_ms, end_ms]`` (inclusivo; ``None`` = sin límite).

    Raises:
        pydantic.ValidationError: SI algún componente de ``dataset`` es un *path traversal*.
        ValueError: SI ``start_ms > end_ms``.
        SourceNotFoundError: SI el dataset no existe bajo ``FT_PARQUET_DIR``.
        InvalidSourceError: SI la fuente no es canónica, sale de ``FT_PARQUET_DIR`` o tiene
            precios nulos, no finitos o ``<= 0`` en el rango.
        EmptyRangeError: SI el rango no contiene velas.
    """
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        msg = f"start_ms ({start_ms}) debe ser <= end_ms ({end_ms})"
        raise ValueError(msg)
    ds = _checked_dataset(settings, dataset)

    source = load_source(settings, ds, PRICE_COLUMNS)
    # Orden, unicidad y dtypes sobre la serie completa: filtrar antes ocultaría el desorden.
    clean = validate_source(source.frame, PRICE_COLUMNS).frame.select(CANDLE_COLUMNS)

    time = pl.col(TIME_COLUMN)
    if start_ms is not None:
        clean = clean.filter(time >= start_ms)
    if end_ms is not None:
        clean = clean.filter(time <= end_ms)
    if clean.is_empty():
        msg = f"empty_range: {ds.path_segment()} no tiene velas en [{start_ms}, {end_ms}]"
        raise EmptyRangeError(msg)
    _check_prices(clean, ds)

    gaps = count_data_gaps(clean[TIME_COLUMN], ds.timeframe)
    logger.debug("candles_loaded", dataset=ds.path_segment(), rows=clean.height, data_gaps=gaps)
    return Candles(dataset=ds, frame=clean, data_gaps=gaps)
