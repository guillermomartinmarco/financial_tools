"""Vista ancha para exportar series persistidas (plan de ``ft_indicators`` §3.7, RF-9).

Tabla: ``exchange``, ``market_type``, ``symbol``, ``timeframe``, ``open_time_ms``,
[OHLCV fuente], columnas de las series; ordenada por ``open_time_ms``. Solo lee lo
persistido (no recalcula) y es genérica sobre el espacio de nombres del almacén, de modo
que ``ft_ta_lib`` la reutiliza tal cual (las columnas ``Int16``/``Int64`` se conservan).
"""

from __future__ import annotations

from typing import NamedTuple

import polars as pl

from finances_core.config import FinancesSettings
from finances_core.indicators_store import source
from finances_core.indicators_store.store import IndicatorStore, SeriesNotFoundError
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import (
    OHLCV_EXPORT_COLUMNS,
    SeriesExportRequest,
    StoreEntry,
)

TIME_COLUMN = "open_time_ms"
STABLE_FLAG_SUFFIX = "_is_stable"


class ExportValidationError(ValueError):
    """Petición de exportación inválida (rango invertido, ``stability=drop`` sin estabilidad)."""


class WideView(NamedTuple):
    """Resultado de :func:`build_wide_view`."""

    frame: pl.LazyFrame
    stale: list[str]  # series cuyo last_ms < último open_time_ms de la fuente
    series: list[str]  # series exportadas, en orden de columnas


def _select_series(
    entries: dict[str, StoreEntry], req: SeriesExportRequest, ds: DatasetIdentifier
) -> tuple[list[str], dict[str, list[str]]]:
    """Series y columnas a exportar (``{series_id: [columnas]}``)."""
    if req.series is not None:
        missing = [s for s in req.series if s not in entries]
        if missing:
            msg = f"Series no calculadas para {ds.path_segment()}: {missing}"
            raise SeriesNotFoundError(msg)
        chosen = list(dict.fromkeys(req.series))
    else:
        chosen = sorted(entries)
    columns = {sid: list(entries[sid].columns) for sid in chosen}
    if req.columns is not None:
        owner = {col: sid for sid in chosen for col in columns[sid]}
        missing = [c for c in req.columns if c not in owner]
        if missing:
            msg = f"Columnas no calculadas para {ds.path_segment()}: {missing}"
            raise SeriesNotFoundError(msg)
        wanted = set(req.columns)
        columns = {sid: [c for c in cols if c in wanted] for sid, cols in columns.items()}
        columns = {sid: cols for sid, cols in columns.items() if cols}
        chosen = [sid for sid in chosen if sid in columns]
    return chosen, columns


def _source_max_ms(settings: FinancesSettings, ds: DatasetIdentifier) -> int | None:
    files = source.source_files(settings, ds)
    if not files:
        return None
    value = (
        pl.scan_parquet(files, hive_partitioning=False)
        .select(pl.col(TIME_COLUMN).max())
        .collect()
        .item()
    )
    return int(value) if value is not None else None


def _range_filter(lf: pl.LazyFrame, req: SeriesExportRequest) -> pl.LazyFrame:
    if req.start_ms is not None:
        lf = lf.filter(pl.col(TIME_COLUMN) >= req.start_ms)
    if req.end_ms is not None:
        lf = lf.filter(pl.col(TIME_COLUMN) < req.end_ms)
    return lf


def build_wide_view(
    store: IndicatorStore,
    ds: DatasetIdentifier,
    req: SeriesExportRequest,
    *,
    settings: FinancesSettings | None = None,
) -> WideView:
    """Tabla ancha *lazy* de las series pedidas de ``ds`` en el namespace de ``store``.

    Raises:
        SeriesNotFoundError: SI el dataset no tiene manifiesto o alguna serie/columna
            pedida no está calculada.
        ExportValidationError: SI ``start_ms >= end_ms`` o ``stability="drop"`` con
            alguna serie sin ``stable_from_ms``.
        SourceNotFoundError: SI ``include_ohlcv`` y la fuente ya no existe.
    """
    settings = settings or FinancesSettings()
    if req.start_ms is not None and req.end_ms is not None and req.start_ms >= req.end_ms:
        msg = f"start_ms ({req.start_ms}) debe ser < end_ms ({req.end_ms}, exclusivo)"
        raise ExportValidationError(msg)
    manifest = store.read_manifest(ds)
    if manifest is None or not manifest.entries:
        msg = f"Sin series calculadas para {ds.path_segment()} ({store.namespace})"
        raise SeriesNotFoundError(msg)
    chosen, columns = _select_series(manifest.entries, req, ds)

    if req.stability == "drop":
        unstable = [s for s in chosen if manifest.entries[s].stability.stable_from_ms is None]
        if unstable:
            msg = f"stability=drop imposible: stable_from_ms nulo en {unstable}"
            raise ExportValidationError(msg)

    scans = {
        sid: store.scan_series(ds, sid, start_ms=req.start_ms, end_ms=req.end_ms).select(
            TIME_COLUMN, *columns[sid]
        )
        for sid in chosen
    }
    ohlcv: list[str] = []
    if req.include_ohlcv:
        available = source.available_columns(settings, ds)
        ohlcv = [c for c in OHLCV_EXPORT_COLUMNS if c in available]
        files = source.source_files(settings, ds)
        base = _range_filter(
            pl.scan_parquet(files, hive_partitioning=False).select(TIME_COLUMN, *ohlcv), req
        )
    else:
        base = (
            pl.concat([lf.select(TIME_COLUMN) for lf in scans.values()], how="vertical")
            .unique()
            .sort(TIME_COLUMN)
        )

    frame = base
    for sid in chosen:
        frame = frame.join(scans[sid], on=TIME_COLUMN, how="left")

    flags: list[str] = []
    if req.stability == "flag":
        for sid in chosen:
            stable = manifest.entries[sid].stability.stable_from_ms
            expr = pl.lit(value=False) if stable is None else pl.col(TIME_COLUMN) >= stable
            flags.append(f"{sid}{STABLE_FLAG_SUFFIX}")
            frame = frame.with_columns(expr.alias(flags[-1]))
    elif req.stability == "drop":
        threshold = max(int(manifest.entries[s].stability.stable_from_ms or 0) for s in chosen)
        frame = frame.filter(pl.col(TIME_COLUMN) >= threshold)

    identity = [
        pl.lit(ds.exchange).alias("exchange"),
        pl.lit(ds.market_type).alias("market_type"),
        pl.lit(ds.symbol).alias("symbol"),
        pl.lit(ds.timeframe).alias("timeframe"),
    ]
    value_columns = [c for sid in chosen for c in columns[sid]]
    frame = frame.select(*identity, TIME_COLUMN, *ohlcv, *value_columns, *flags).sort(TIME_COLUMN)

    source_max = _source_max_ms(settings, ds)
    stale = []
    if source_max is not None:
        stale = [
            sid
            for sid in chosen
            if (last := manifest.entries[sid].last_ms) is None or last < source_max
        ]
    return WideView(frame=frame, stale=stale, series=chosen)
