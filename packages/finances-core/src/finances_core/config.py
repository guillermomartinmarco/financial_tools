import pathlib
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class FinancesSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Directorios base
    DATA_DIR: pathlib.Path = pathlib.Path("./data")
    RAW_DIR: pathlib.Path = pathlib.Path("./data/raw")
    PROCESSED_DIR: pathlib.Path = pathlib.Path("./data/processed")
    PARQUET_DIR: pathlib.Path = pathlib.Path("./data/parquet")
    LOGS_DIR: pathlib.Path = pathlib.Path("./logs")
    STRATEGIES_DIR: pathlib.Path = pathlib.Path("./strategies")
    RESULTS_DIR: pathlib.Path = pathlib.Path("./results")
    SHARE_DIR: pathlib.Path = pathlib.Path("./share")

    # SQLite WAL
    TASKS_DB_PATH: pathlib.Path = pathlib.Path("./share/tasks.db")

    # API Service
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_WORKERS: int = 1

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "rich"] = "json"

    # Binance Public Data
    BINANCE_PUBLIC_DATA_BASE_URL_SPOT: str = "https://data.binance.vision/data/spot"
    BINANCE_PUBLIC_DATA_BASE_URL_FUTURES: str = "https://data.binance.vision/data/futures/um"
    BINANCE_PUBLIC_DATA_RATE_CAPACITY: int = 50
    BINANCE_PUBLIC_DATA_RATE_REFILL_RATE: float = 10.0

    # Binance REST API
    BINANCE_REST_BASE_URL_SPOT: str = "https://api.binance.com"
    BINANCE_REST_BASE_URL_FUTURES: str = "https://fapi.binance.com"
    BINANCE_REST_RATE_CAPACITY: int = 1200
    BINANCE_REST_RATE_REFILL_RATE: float = 20.0
    BINANCE_MAX_RETRIES: int = 3
    BINANCE_BASE_BACKOFF_MS: int = 1000

    # Secrets (se enmascaran automáticamente por nombre)
    BINANCE_API_KEY: str | None = None
    BINANCE_API_SECRET: str | None = None

    # Concurrencia
    DOWNLOAD_MAX_CONCURRENT: int = 5

    # Otros exchanges (futuro)
    YFINANCE_API_KEY: str | None = None

    # Data Viewer
    DATA_VIEWER_SAMPLE_MAX_ROWS: int = 10000
    DATA_VIEWER_GAP_THRESHOLD_MS: int = 0


settings = FinancesSettings()
