from pathlib import Path

import polars as pl
import structlog

logger = structlog.get_logger(__name__)


def write_parquet_partitioned(df: pl.DataFrame, parquet_dir: Path) -> list[str]:
    """
    Escribe DataFrame particionado manualmente para evitar problemas con partition_by en Windows.
    Layout: {parquet_dir}/binance/market_type={market_type}/symbol={symbol}/timeframe={tf}/year={yyyy}/month={mm}/data.parquet
    """
    if df.height == 0:
        logger.warning("write_parquet_empty_dataframe")
        return []

    written_paths = []

    # Agrupar por todas las claves de partición
    partition_cols = ["market_type", "symbol", "timeframe", "year", "month"]
    
    for partition_keys, group_df in df.group_by(partition_cols, maintain_order=True):
        market_type, symbol, timeframe, year, month = partition_keys
        
        # Construir path de destino con formato Hive (key=value)
        partition_path = (
            parquet_dir 
            / "binance" 
            / f"market_type={market_type}" 
            / f"symbol={symbol}" 
            / f"timeframe={timeframe}" 
            / f"year={year}" 
            / f"month={month:02d}"
        )
        partition_path.mkdir(parents=True, exist_ok=True)
        
        # Escribir archivo Parquet (sin columnas de partición en el archivo)
        output_file = partition_path / "data.parquet"
        
        # Eliminar columnas de partición del DataFrame a escribir
        write_df = group_df.drop(partition_cols)
        
        write_df.write_parquet(
            output_file,
            compression="zstd",
            compression_level=3,
            statistics=True,
            use_pyarrow=True,
        )
        
        written_paths.append(str(output_file))

    logger.info("parquet_written", files=len(written_paths), rows=df.height, base=str(parquet_dir / "binance"))
    return written_paths
