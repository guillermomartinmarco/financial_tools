import asyncio
import json
import tempfile
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import polars as pl
import structlog
from finances_core.config import settings
from finances_core.schemas.market_data import DownloadPlan, DownloadPlanItem, DownloadResult
from finances_core.tasks import SQLTaskManager, now_ms

from finances_downloader.ft_market_data_binance.client import (
    BinancePublicDataClient,
    BinanceRestClient,
)
from finances_downloader.ft_market_data_binance.normalizer import (
    normalize_rest_json,
    read_public_data_csv,
)
from finances_downloader.ft_market_data_binance.rate_limiter import DualTokenBucket
from finances_downloader.ft_market_data_binance.writer import write_parquet_partitioned

logger = structlog.get_logger(__name__)


async def execute_download_plan(plan: DownloadPlan, max_concurrent: int = 5) -> DownloadResult:
    """
    Orquestador principal - Paso 7 del pipeline:
    Ejecuta plan de descarga con concurrencia controlada.
    """
    task_id = plan.task_id
    semaphore = asyncio.Semaphore(max_concurrent)
    task_manager = SQLTaskManager(settings.TASKS_DB_PATH)

    # Crear tarea si no existe (idempotente)
    task_manager.create_task(task_id, status="PENDING", phase="PENDING")
    task_manager.update_task(task_id, status="PLANNING", phase="PLANNING")

    # Agrupar items por source
    public_items = [item for item in plan.items if item.source == "public_data"]
    rest_items = [item for item in plan.items if item.source == "rest_api"]

    # Gaps encontrados = número de combinaciones únicas (symbol, tf, market) con items
    gaps_found = len(set((item.symbol, item.timeframe, item.market_type) for item in plan.items))
    task_manager.update_task(task_id, gaps_found=gaps_found)

    all_dataframes: dict[tuple[str, str, str], list[pl.DataFrame]] = defaultdict(list)
    stats: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: {"candles": 0, "bytes": 0, "public": 0, "rest": 0})
    errors = []
    gaps_filled_public = 0
    gaps_filled_rest = 0
    gaps_unfillable: list[dict[str, object]] = []

    # Un solo rate limiter compartido para todo el plan
    rate_limiter = DualTokenBucket(
        settings.BINANCE_PUBLIC_DATA_RATE_CAPACITY,
        settings.BINANCE_PUBLIC_DATA_RATE_REFILL_RATE,
        settings.BINANCE_REST_RATE_CAPACITY,
        settings.BINANCE_REST_RATE_REFILL_RATE,
    )

    async def process_public_item(item: DownloadPlanItem) -> None:
        nonlocal gaps_filled_public
        async with semaphore:
            task_manager.update_task(task_id, status="DOWNLOADING_PUBLIC", phase="DOWNLOADING_PUBLIC",
                                   current_symbol=item.symbol, current_timeframe=item.timeframe)
            try:
                async with BinancePublicDataClient(rate_limiter) as client:
                    year, month = ms_to_year_month(item.covers_range_ms[0])
                    zip_path = await client.download_monthly(
                        item.symbol, item.timeframe, item.market_type,
                        year, month,
                        item.expected_checksum
                    )

                    # Leer y normalizar CSV del zip
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with zipfile.ZipFile(zip_path, 'r') as zf:
                            zf.extractall(tmpdir)
                        csv_files = list(Path(tmpdir).glob("*.csv"))
                        if csv_files:
                            df = read_public_data_csv(
                                str(csv_files[0]), item.symbol, item.timeframe, item.market_type,
                                item.covers_range_ms[0], item.covers_range_ms[1]
                            )
                            key = (item.symbol, item.timeframe, item.market_type)
                            all_dataframes[key].append(df)
                            stats[key]["candles"] += df.height
                            stats[key]["bytes"] += zip_path.stat().st_size
                            stats[key]["public"] += 1
                            gaps_filled_public += 1
                            task_manager.update_task(task_id,
                                processed_candles=sum(s["candles"] for s in stats.values()),
                                downloaded_bytes=sum(s["bytes"] for s in stats.values()),
                                gaps_filled_public=gaps_filled_public
                            )
            except Exception as e:
                error_msg = f"Public Data {item.symbol}/{item.timeframe}: {e}"
                errors.append(error_msg)
                gaps_unfillable.append({"symbol": item.symbol, "timeframe": item.timeframe, "market_type": item.market_type, "error": str(e)})
                logger.error("public_item_failed", item=item.model_dump(), error=str(e))
            finally:
                if 'zip_path' in locals() and zip_path.exists():
                    zip_path.unlink()

    async def process_rest_item(item: DownloadPlanItem) -> None:
        nonlocal gaps_filled_rest
        async with semaphore:
            task_manager.update_task(task_id, status="DOWNLOADING_REST", phase="DOWNLOADING_REST",
                                   current_symbol=item.symbol, current_timeframe=item.timeframe)
            try:
                async with BinanceRestClient(rate_limiter) as client:
                    params = item.params or {}
                    start_time = int(str(params["startTime"]))
                    end_time = int(str(params["endTime"]))
                    limit = int(str(params["limit"]))
                    klines = await client.fetch_klines(
                        item.symbol, item.timeframe, item.market_type,
                        start_time, end_time, limit
                    )
                    if klines:
                        df = normalize_rest_json(
                            klines, item.symbol, item.timeframe, item.market_type,
                            item.covers_range_ms[0], item.covers_range_ms[1]
                        )
                        key = (item.symbol, item.timeframe, item.market_type)
                        all_dataframes[key].append(df)
                        stats[key]["candles"] += df.height
                        stats[key]["rest"] += 1
                        gaps_filled_rest += 1
                        task_manager.update_task(task_id,
                            processed_candles=sum(s["candles"] for s in stats.values()),
                            gaps_filled_rest=gaps_filled_rest
                        )
            except Exception as e:
                error_msg = f"REST {item.symbol}/{item.timeframe}: {e}"
                errors.append(error_msg)
                gaps_unfillable.append({"symbol": item.symbol, "timeframe": item.timeframe, "market_type": item.market_type, "error": str(e)})
                logger.error("rest_item_failed", item=item.model_dump(), error=str(e))

    # Ejecutar todos los items en paralelo
    tasks = [process_public_item(item) for item in public_items]
    tasks += [process_rest_item(item) for item in rest_items]

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Normalizar y escribir
    task_manager.update_task(task_id, status="NORMALIZING", phase="NORMALIZING")

    final_dfs = []
    for key, dfs in all_dataframes.items():
        if dfs:
            combined = pl.concat(dfs).unique(subset=["open_time_ms"], maintain_order=True).sort("open_time_ms")
            final_dfs.append(combined)

    task_manager.update_task(task_id, status="WRITING_PARQUET", phase="WRITING_PARQUET")

    parquet_paths = []
    for df in final_dfs:
        if df.height > 0:
            paths = write_parquet_partitioned(df, settings.PARQUET_DIR)
            parquet_paths.extend(paths)

    # Resultado final: FAILED si no hay datos y hay errores; COMPLETED si hay datos (aunque haya errores parciales)
    has_data = len(parquet_paths) > 0
    final_status: Literal['COMPLETED', 'FAILED'] = "COMPLETED" if has_data else ("FAILED" if errors else "COMPLETED")

    result = DownloadResult(
        task_id=task_id,
        status=final_status,
        parquet_paths=parquet_paths,
        stats={f"{k[0]}/{k[1]}/{k[2]}": v for k, v in stats.items()},
        gaps_unfillable=gaps_unfillable,
    )

    task_manager.update_task(
        task_id,
        status=result.status,
        phase=result.status,
        parquet_paths_json=json.dumps(parquet_paths),
        stats_json=json.dumps({f"{k[0]}/{k[1]}/{k[2]}": v for k, v in stats.items()}),
        errors_json=json.dumps(errors),
        finished_at_ms=now_ms(),
    )

    return result


def ms_to_year_month(ms: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.year, dt.month
