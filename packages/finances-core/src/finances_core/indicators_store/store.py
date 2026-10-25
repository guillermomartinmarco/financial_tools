"""Almacén de series por mes bajo ``FT_INDICATORS_DIR`` (plan de ``ft_indicators`` §3.6).

Layout (mismas claves Hive que la fuente; ADR-I05)::

    {root}/{exchange}/market_type={X}/symbol={Y}/timeframe={Z}/year={YYYY}/month={MM}/{series_id}.parquet

Cada archivo contiene ``open_time_ms`` (``Int64``) + las columnas de salida con el dtype
que decide la herramienta (``Float32``; ``ft_ta_lib`` también ``Int16``/``Int64``,
Art. 5.1 bis). Cada escritura **reescribe completa** la serie: cada mes se reemplaza de
forma atómica y los archivos de meses sin filas se eliminan; las demás series no se tocan.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import structlog

from finances_core.config import FinancesSettings
from finances_core.indicators_store import manifest as manifest_mod
from finances_core.indicators_store.namespaces import validate_series_id
from finances_core.marketdata.hive_writer import write_parquet_partitioned
from finances_core.marketdata.inventory import partition_base_dir
from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.indicators import ColumnDtype, Namespace, StoreEntry, StoreManifest

logger = structlog.get_logger(__name__)

TIME_COLUMN = "open_time_ms"
#: Dtypes persistibles (Art. 5.1 / 5.1 bis): nunca ``Float64``.
ALLOWED_DTYPES: dict[str, ColumnDtype] = {
    "Float32": "Float32",
    "Int16": "Int16",
    "Int64": "Int64",
}
_INT16_RANGE = (-32_768, 32_767)

DtypeLike = pl.DataType | type[pl.DataType]


class SeriesNotFoundError(LookupError):
    """La serie (o el dataset) no está calculada en el almacén."""


class SeriesDtypeError(ValueError):
    """Dtype no persistible o valor fuera del dominio del tipo (nunca se trunca)."""


@dataclass(frozen=True)
class WriteStats:
    """Resultado de :meth:`IndicatorStore.write_series`."""

    series_id: str
    rows: int
    first_ms: int | None
    last_ms: int | None
    partitions_written: int
    partitions_removed: int
    files: list[str] = field(default_factory=list)


def dtype_name(dtype: DtypeLike) -> ColumnDtype:
    """Nombre canónico (``Float32``/``Int16``/``Int64``) de un dtype persistible.

    Raises:
        SeriesDtypeError: SI el dtype no se puede persistir (p. ej. ``Float64``).
    """
    name = str(dtype() if isinstance(dtype, type) else dtype)
    try:
        return ALLOWED_DTYPES[name]
    except KeyError:
        msg = f"dtype {name} no persistible; permitidos: {sorted(ALLOWED_DTYPES)} (Art. 5)"
        raise SeriesDtypeError(msg) from None


def _month_key(path: Path) -> tuple[int, int] | None:
    try:
        year = int(path.parent.parent.name.removeprefix("year="))
        month = int(path.parent.name.removeprefix("month="))
    except ValueError:
        return None
    return year, month


def _ms_month(ms: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.year, dt.month


class IndicatorStore:
    """Almacén de un espacio de nombres (``ft_indicators`` o ``ft_ta_lib``) bajo ``root``."""

    def __init__(
        self, root: Path, namespace: Namespace, *, tasks_db_path: Path | None = None
    ) -> None:
        self.root = Path(root)
        self.namespace: Namespace = namespace
        self._tasks_db_path = tasks_db_path

    @classmethod
    def from_settings(cls, settings: FinancesSettings, namespace: Namespace) -> IndicatorStore:
        """Almacén en ``settings.INDICATORS_DIR`` validado (RF-7).

        Raises:
            ConfigError: SI ``INDICATORS_DIR`` está dentro de ``PARQUET_DIR``.
        """
        root = settings.require_indicators_dir()
        return cls(root, namespace, tasks_db_path=settings.TASKS_DB_PATH)

    # ----------------------------------------------------------------- rutas
    def dataset_dir(self, ds: DatasetIdentifier) -> Path:
        """``{root}/{exchange}/market_type=…/symbol=…/timeframe=…``."""
        return partition_base_dir(self.root, ds.exchange, ds.symbol, ds.timeframe, ds.market_type)

    def series_files(self, ds: DatasetIdentifier, series_id: str) -> list[Path]:
        """Archivos mensuales de la serie en orden cronológico."""
        base = self.dataset_dir(ds)
        if not base.is_dir():
            return []
        return sorted(p for p in base.glob(f"year=*/month=*/{series_id}.parquet") if p.is_file())

    def has_series(self, ds: DatasetIdentifier, series_id: str) -> bool:
        return bool(self.series_files(ds, series_id))

    # -------------------------------------------------------------- escritura
    def _prepare(
        self,
        series_id: str,
        df: pl.DataFrame,
        dtypes: Mapping[str, DtypeLike],
    ) -> pl.DataFrame:
        validate_series_id(self.namespace, series_id)
        if TIME_COLUMN not in df.columns:
            msg = f"La serie {series_id!r} no tiene {TIME_COLUMN}"
            raise ValueError(msg)
        missing = [c for c in dtypes if c not in df.columns]
        if missing:
            msg = f"Columnas de salida ausentes en {series_id!r}: {missing}"
            raise ValueError(msg)
        exprs: list[pl.Expr] = [pl.col(TIME_COLUMN).cast(pl.Int64)]
        for column, dtype in dtypes.items():
            validate_series_id(self.namespace, column)
            target = dtype_name(dtype)
            src = df[column]
            if target != "Float32":
                self._check_integral(series_id, column, src, target)
            exprs.append(pl.col(column).cast(getattr(pl, target)))
        try:
            return df.select(exprs)
        except pl.exceptions.PolarsError as exc:
            msg = f"No se puede convertir {series_id!r} a los dtypes pedidos: {exc}"
            raise SeriesDtypeError(msg) from exc

    @staticmethod
    def _check_integral(series_id: str, column: str, src: pl.Series, target: str) -> None:
        values = src.drop_nulls()
        if values.len() == 0:
            return
        if src.dtype.is_float() and not bool((values == values.round(0)).all()):
            msg = f"{series_id}.{column}: {target} exige valores enteros (sin truncar)"
            raise SeriesDtypeError(msg)
        if target == "Int16":
            lo, hi = _INT16_RANGE
            if bool(((values < lo) | (values > hi)).any()):
                msg = f"{series_id}.{column}: valores fuera de Int16 [{lo}, {hi}] (Art. 5.1 bis)"
                raise SeriesDtypeError(msg)

    def write_series(
        self,
        ds: DatasetIdentifier,
        series_id: str,
        df: pl.DataFrame,
        dtypes: Mapping[str, DtypeLike],
    ) -> WriteStats:
        """Reescribe completa la serie ``series_id`` de ``ds``.

        ``df`` contiene ``open_time_ms`` + las columnas de ``dtypes`` con **una fila por
        fila fuente** (alineación 1:1): cada mes se reemplaza atómicamente con sus filas y
        los ``{series_id}.parquet`` de meses sin filas se eliminan.

        Raises:
            NamespaceViolationError: SI la serie o sus columnas no son del namespace.
            SeriesDtypeError: SI un dtype no es persistible o un valor no cabe en él.
            OSError: ante errores de escritura (cada archivo queda en su versión previa
                o en la nueva, nunca a medias).
        """
        data = self._prepare(series_id, df, dtypes)
        schema: dict[str, DtypeLike] = {
            "market_type": pl.Utf8,
            "symbol": pl.Utf8,
            "timeframe": pl.Utf8,
            TIME_COLUMN: pl.Int64,
            **{c: getattr(pl, dtype_name(t)) for c, t in dtypes.items()},
        }
        written: list[str] = []
        if data.height:
            dt = pl.from_epoch(pl.col(TIME_COLUMN), time_unit="ms")
            framed = data.with_columns(
                pl.lit(ds.market_type).alias("market_type"),
                pl.lit(ds.symbol).alias("symbol"),
                pl.lit(ds.timeframe).alias("timeframe"),
                dt.dt.year().alias("year"),
                dt.dt.month().alias("month"),
            )
            written = write_parquet_partitioned(
                framed,
                self.root,
                exchange=ds.exchange,
                schema=schema,
                file_name=f"{series_id}.parquet",
                mode="replace",
            )
        keep = {Path(p).resolve() for p in written}
        removed = 0
        for path in self.series_files(ds, series_id):
            if path.resolve() not in keep:
                path.unlink(missing_ok=True)
                removed += 1
                self._prune_empty_dirs(path.parent)
        first_ms = last_ms = None
        if data.height:
            bounds = data.select(
                pl.col(TIME_COLUMN).min().alias("lo"), pl.col(TIME_COLUMN).max().alias("hi")
            )
            first_ms, last_ms = int(bounds.item(0, "lo")), int(bounds.item(0, "hi"))
        stats = WriteStats(
            series_id=series_id,
            rows=data.height,
            first_ms=first_ms,
            last_ms=last_ms,
            partitions_written=len(written),
            partitions_removed=removed,
            files=written,
        )
        logger.debug(
            "series_written",
            namespace=self.namespace,
            dataset=ds.path_segment(),
            series_id=series_id,
            rows=stats.rows,
            partitions_written=stats.partitions_written,
            partitions_removed=removed,
        )
        return stats

    def _prune_empty_dirs(self, month_dir: Path) -> None:
        for directory in (month_dir, month_dir.parent):
            try:
                directory.rmdir()  # solo si está vacío
            except OSError:
                return

    # ---------------------------------------------------------------- lectura
    def scan_series(
        self,
        ds: DatasetIdentifier,
        series_id: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> pl.LazyFrame:
        """``LazyFrame`` de la serie con poda por año/mes y filtro ``[start_ms, end_ms)``.

        Raises:
            SeriesNotFoundError: SI la serie no está calculada para ``ds``.
        """
        files = self.series_files(ds, series_id)
        if not files:
            msg = f"Serie {series_id!r} no calculada para {ds.path_segment()} ({self.namespace})"
            raise SeriesNotFoundError(msg)
        lo = _ms_month(start_ms) if start_ms is not None else None
        hi = _ms_month(end_ms - 1) if end_ms is not None else None
        selected = [
            f
            for f in files
            if (key := _month_key(f)) is not None
            and (lo is None or key >= lo)
            and (hi is None or key <= hi)
        ]
        lf = pl.scan_parquet(selected or files[:1], hive_partitioning=False)
        if not selected:
            return lf.filter(pl.lit(value=False))
        if start_ms is not None:
            lf = lf.filter(pl.col(TIME_COLUMN) >= start_ms)
        if end_ms is not None:
            lf = lf.filter(pl.col(TIME_COLUMN) < end_ms)
        return lf

    # ------------------------------------------------------ manifiesto y bloqueo
    def read_manifest(self, ds: DatasetIdentifier) -> StoreManifest | None:
        return manifest_mod.read_manifest(self.dataset_dir(ds), self.namespace)

    def write_manifest(self, ds: DatasetIdentifier, manifest: StoreManifest) -> None:
        if manifest.namespace != self.namespace:
            msg = f"Manifiesto de {manifest.namespace!r} en un almacén de {self.namespace!r}"
            raise ValueError(msg)
        manifest_mod.write_manifest(self.dataset_dir(ds), manifest)

    def update_manifest(
        self, ds: DatasetIdentifier, entries: Mapping[str, StoreEntry]
    ) -> StoreManifest:
        """Actualiza solo ``entries`` (tras escribir las particiones) y conserva el resto."""
        for sid in entries:
            validate_series_id(self.namespace, sid)
        return manifest_mod.merge_entries(self.dataset_dir(ds), self.namespace, ds, entries)

    def lock(self, ds: DatasetIdentifier, task_id: str) -> AbstractContextManager[None]:
        """Bloqueo ``_lock.{namespace}`` del dataset para ``task_id``.

        Raises:
            DatasetLockedError: SI otra tarea viva de este namespace lo tiene.
        """
        db = self._tasks_db_path or FinancesSettings().TASKS_DB_PATH
        return manifest_mod.dataset_lock(
            self.dataset_dir(ds), self.namespace, ds, task_id, manifest_mod.task_liveness(db)
        )
