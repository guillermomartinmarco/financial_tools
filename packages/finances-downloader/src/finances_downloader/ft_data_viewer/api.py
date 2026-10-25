from fastapi import APIRouter, HTTPException, Query, Path, status
from fastapi.responses import StreamingResponse
from typing import Optional, List, Literal
import io
import structlog

from finances_core.schemas.data_viewer import (
    DatasetListResponse, DatasetSummary, GapInfo, AnomalyReport,
    ExportRequest, CompareRequest, CompareResult, DatasetIdentifier
)
from finances_downloader.ft_data_viewer.service import (
    list_datasets, get_summary, get_rows, get_gaps, get_anomalies,
    export_data, compare_datasets_service, DatasetNotFoundError
)

router = APIRouter(prefix="/data", tags=["data-viewer"])
logger = structlog.get_logger(__name__)


@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets_endpoint(
    exchange: Optional[str] = Query(None),
    market_type: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    timeframe: Optional[str] = Query(None),
) -> DatasetListResponse:
    """Lista datasets OHLCV disponibles con filtros opcionales."""
    return list_datasets(exchange, market_type, symbol, timeframe)


@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/summary", response_model=DatasetSummary)
async def get_summary_endpoint(
    exchange: str = Path(..., description="Exchange (binance)"),
    market_type: str = Path(..., description="Market type (spot/futures)"),
    symbol: str = Path(..., description="Symbol (ej. BTCUSDT)"),
    timeframe: str = Path(..., description="Timeframe (ej. 1h)"),
) -> DatasetSummary:
    """Resumen estadístico de un dataset (metadata-only, rápido)."""
    try:
        return get_summary(exchange, market_type, symbol, timeframe)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/rows")
async def get_rows_endpoint(
    exchange: str = Path(..., description="Exchange"),
    market_type: str = Path(..., description="Market type"),
    symbol: str = Path(..., description="Symbol"),
    timeframe: str = Path(..., description="Timeframe"),
    mode: Literal["head", "tail", "sample"] = Query("head"),
    n: int = Query(10, ge=1, le=10000),
    cols: Optional[str] = Query(None, description="Columnas separadas por coma"),
) -> List[dict]:
    """Obtiene filas muestra (head/tail/sample)."""
    try:
        col_list = cols.split(",") if cols else None
        rows = get_rows(exchange, market_type, symbol, timeframe, mode, n, col_list)
        return rows
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/gaps")
async def get_gaps_endpoint(
    exchange: str = Path(..., description="Exchange"),
    market_type: str = Path(..., description="Market type"),
    symbol: str = Path(..., description="Symbol"),
    timeframe: str = Path(..., description="Timeframe"),
    min_gap_candles: int = Query(1, ge=1),
) -> List[dict]:
    """Detecta gaps temporales en el dataset."""
    try:
        gaps = get_gaps(exchange, market_type, symbol, timeframe, min_gap_candles)
        return gaps
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/anomalies", response_model=AnomalyReport)
async def get_anomalies_endpoint(
    exchange: str = Path(..., description="Exchange"),
    market_type: str = Path(..., description="Market type"),
    symbol: str = Path(..., description="Symbol"),
    timeframe: str = Path(..., description="Timeframe"),
) -> AnomalyReport:
    """Detecta anomalías: duplicados, nulos, outliers precio/volumen."""
    try:
        return get_anomalies(exchange, market_type, symbol, timeframe)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/datasets/{exchange}/{market_type}/{symbol}/{timeframe}/export")
async def export_data_endpoint(
    exchange: str = Path(..., description="Exchange"),
    market_type: str = Path(..., description="Market type"),
    symbol: str = Path(..., description="Symbol"),
    timeframe: str = Path(..., description="Timeframe"),
    request: ExportRequest = ...,
) -> StreamingResponse:
    """
    Exporta subconjunto filtrado a Parquet/CSV/JSONL.
    Retorna StreamingResponse para descarga.
    """
    try:
        request.dataset_id = DatasetIdentifier(
            exchange=exchange.lower(),
            market_type=market_type,
            symbol=symbol.upper(),
            timeframe=timeframe,
        )
        result = export_data(
            exchange, market_type, symbol, timeframe,
            request.start_ms, request.end_ms,
            request.columns, request.format, request.output_dir
        )

        if isinstance(result, int):
            # Exportado a stdout - no aplica en API
            raise HTTPException(status_code=500, detail="Output dir required for API export")

        output_path = result
        media_type = {
            "parquet": "application/octet-stream",
            "csv": "text/csv",
            "jsonl": "application/x-ndjson",
        }.get(request.format, "application/octet-stream")

        filename = f"{exchange}_{market_type}_{symbol}_{timeframe}.{request.format}"

        def iterfile():
            with open(result, "rb") as f:
                yield from f

        return StreamingResponse(
            iterfile(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/compare", response_model=CompareResult)
async def compare_datasets_endpoint(request: CompareRequest) -> CompareResult:
    """Compara dos datasets."""
    try:
        return compare_datasets_service(request.dataset_a, request.dataset_b, request.start_ms, request.end_ms)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))