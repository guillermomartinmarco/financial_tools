"""Router FastAPI de ft_data_viewer (adaptador delgado sobre ``service``, Art. 1).

Se exporta ``router`` (prefijo ``/data``); lo monta el paquete ``finances-api``.
Todo trabajo Polars/IO se despacha con ``run_in_threadpool`` (Art. 3).
Errores: no encontrado → 404 · argumentos inválidos / sin datos → 400 ·
validación de esquema → 422 (FastAPI) · interno → 500.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from finances_core.schemas.data_viewer import (
    AnomalyReport,
    CompareRequest,
    CompareResult,
    DatasetIdentifier,
    DatasetListResponse,
    DatasetSummary,
    ExportRequest,
    GapInfo,
)
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from finances_downloader.ft_data_viewer import service
from finances_downloader.ft_data_viewer.analyzer import OutlierMethod, SampleMode
from finances_downloader.ft_data_viewer.errors import (
    DatasetNotFoundError,
    InvalidArgumentError,
    NoDataInRangeError,
)
from finances_downloader.ft_data_viewer.exporter import MEDIA_TYPES
from finances_downloader.ft_data_viewer.scanner import MARKET_HELP

router = APIRouter(prefix="/data", tags=["data-viewer"])
logger = structlog.get_logger(__name__, component="ft_data_viewer")

T = TypeVar("T")

_CHUNK_SIZE = 1024 * 1024

ExchangeParam = PathParam(..., description="Exchange (ej. binance)")
MarketParam = PathParam(..., description=f"Market type ({MARKET_HELP})")
SymbolParam = PathParam(..., description="Símbolo (ej. BTCUSDT)")
TimeframeParam = PathParam(..., description="Timeframe (ej. 1h)")


async def _run(func: Callable[[], T]) -> T:
    """Ejecuta ``func`` en el threadpool con ``task_id`` en contexto y mapea errores a HTTP."""
    task_id = uuid4().hex

    def call() -> T:
        with structlog.contextvars.bound_contextvars(task_id=task_id):
            return func()

    try:
        return await run_in_threadpool(call)
    except DatasetNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (InvalidArgumentError, NoDataInRangeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("data_viewer_request_failed", task_id=task_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno de ft_data_viewer"
        ) from exc


def _ident(exchange: str, market_type: str, symbol: str, timeframe: str) -> DatasetIdentifier:
    return service.make_identifier(exchange, market_type, symbol, timeframe)


@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets(
    exchange: str | None = Query(None),
    market_type: str | None = Query(None),
    symbol: str | None = Query(None),
    timeframe: str | None = Query(None),
) -> DatasetListResponse:
    """Lista datasets OHLCV disponibles con filtros opcionales (RF-1)."""
    return await _run(lambda: service.list_datasets(exchange, market_type, symbol, timeframe))


@router.get(
    "/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/summary",
    response_model=DatasetSummary,
)
async def get_summary(
    exchange: str = ExchangeParam,
    market_type: str = MarketParam,
    symbol: str = SymbolParam,
    timeframe: str = TimeframeParam,
) -> DatasetSummary:
    """Resumen estadístico de un dataset (RF-2)."""
    return await _run(lambda: service.get_summary(_ident(exchange, market_type, symbol, timeframe)))


@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/rows")
async def get_rows(
    exchange: str = ExchangeParam,
    market_type: str = MarketParam,
    symbol: str = SymbolParam,
    timeframe: str = TimeframeParam,
    mode: SampleMode = Query("head"),
    n: int = Query(10, ge=1, le=10000),
    cols: str | None = Query(None, description="Columnas separadas por coma"),
) -> list[dict[str, Any]]:
    """Filas ``head``/``tail``/``sample`` (RF-3)."""
    return await _run(
        lambda: service.get_rows(
            _ident(exchange, market_type, symbol, timeframe), mode, n, service.parse_columns(cols)
        )
    )


@router.get(
    "/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/gaps",
    response_model=list[GapInfo],
)
async def get_gaps(
    exchange: str = ExchangeParam,
    market_type: str = MarketParam,
    symbol: str = SymbolParam,
    timeframe: str = TimeframeParam,
    min_gap_candles: int = Query(1, ge=1),
) -> list[GapInfo]:
    """Gaps temporales del dataset (RF-4)."""
    return await _run(
        lambda: service.get_gaps(_ident(exchange, market_type, symbol, timeframe), min_gap_candles)
    )


@router.get(
    "/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/anomalies",
    response_model=AnomalyReport,
)
async def get_anomalies(
    exchange: str = ExchangeParam,
    market_type: str = MarketParam,
    symbol: str = SymbolParam,
    timeframe: str = TimeframeParam,
    outlier_method: OutlierMethod = Query("zscore"),
) -> AnomalyReport:
    """Duplicados, nulos, outliers de precio y volumen (RF-5)."""
    return await _run(
        lambda: service.get_anomalies(
            _ident(exchange, market_type, symbol, timeframe), outlier_method
        )
    )


def _iter_file(path: Path) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            yield chunk


def _exported_path(outcome: service.ExportOutcome) -> Path:
    if outcome.path is None:  # export_data con output_dir siempre devuelve ruta
        msg = "export_data no devolvió ruta de salida"
        raise RuntimeError(msg)
    return outcome.path


def _export_to_file(request: ExportRequest) -> tuple[Path, Path | None]:
    """Exporta al ``output_dir`` pedido o a un directorio temporal (devuelto para limpieza)."""
    if request.output_dir:
        return _exported_path(service.export_data(request)), None
    tmp_dir = Path(tempfile.mkdtemp(prefix="ft_data_viewer_export_"))
    try:
        outcome = service.export_data(request.model_copy(update={"output_dir": str(tmp_dir)}))
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return _exported_path(outcome), tmp_dir


@router.post("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export")
async def export_data(
    request: ExportRequest,
    exchange: str = ExchangeParam,
    market_type: str = MarketParam,
    symbol: str = SymbolParam,
    timeframe: str = TimeframeParam,
) -> StreamingResponse:
    """Exporta un subconjunto filtrado y lo devuelve como descarga streaming (RF-6, T6.3).

    El archivo se genera con ``sink_*`` (streaming) en ``output_dir`` o en un directorio
    temporal que se elimina al terminar la respuesta.
    """

    def work() -> tuple[DatasetIdentifier, Path, Path | None]:
        ident = _ident(exchange, market_type, symbol, timeframe)
        if (
            request.dataset_id is not None
            and service.normalize_identifier(request.dataset_id) != ident
        ):
            msg = "dataset_id del cuerpo no coincide con la ruta"
            raise InvalidArgumentError(msg)
        path, tmp_dir = _export_to_file(request.model_copy(update={"dataset_id": ident}))
        return ident, path, tmp_dir

    ident, path, tmp_dir = await _run(work)
    cleanup = BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True) if tmp_dir else None
    filename = service.export_file_name(ident, request.format)
    return StreamingResponse(
        _iter_file(path),
        media_type=MEDIA_TYPES[request.format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=cleanup,
    )


@router.post("/compare", response_model=CompareResult)
async def compare_datasets(request: CompareRequest) -> CompareResult:
    """Compara dos datasets (RF-7)."""
    return await _run(
        lambda: service.compare_datasets_service(
            service.normalize_identifier(request.dataset_a),
            service.normalize_identifier(request.dataset_b),
            request.start_ms,
            request.end_ms,
        )
    )
