"""Configuración global de la suite (Pydantic Settings, prefijo ``FT_``).

Todas las rutas relativas se resuelven contra la raíz del proyecto y nunca contra
el directorio de trabajo actual (Art. 2), de modo que cualquier herramienta usa la
misma ``share/tasks.db`` y los mismos directorios de datos.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Archivo que marca la raíz del proyecto (lo crea ``ft_setup init``).
ROOT_MARKER = ".ft_root"


class ConfigError(Exception):
    """Configuración incoherente detectada al usarla (p. ej. RF-7 de ``ft_indicators``)."""


def find_project_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """Devuelve la raíz del proyecto como ruta absoluta.

    Prioridad: ``FT_PROJECT_ROOT`` > primer ancestro (incluido ``start``) que
    contenga ``.ft_root`` > ``start`` (por defecto, el directorio actual).
    """
    env_root = os.environ.get("FT_PROJECT_ROOT")
    if env_root:
        return pathlib.Path(env_root).resolve()
    origin = (start or pathlib.Path.cwd()).resolve()
    for candidate in (origin, *origin.parents):
        if (candidate / ROOT_MARKER).is_file():
            return candidate
    return origin


class FinancesSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FT_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **values: Any) -> None:
        # El .env se lee desde la raíz del proyecto, no desde el directorio actual.
        values.setdefault("_env_file", find_project_root() / ".env")
        super().__init__(**values)

    # Raíz del proyecto (base de todas las rutas relativas)
    PROJECT_ROOT: pathlib.Path = Field(default_factory=find_project_root)

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

    # Yahoo Finance (ft_market_data_yfinance; Yahoo no usa API key)
    YFINANCE_RATE_CAPACITY: int = 5
    YFINANCE_RATE_REFILL_RATE: float = 1.0
    YFINANCE_MAX_RETRIES: int = 3
    YFINANCE_BASE_BACKOFF_MS: int = 2000
    YFINANCE_TIMEOUT_S: int = 30

    # Data Viewer
    DATA_VIEWER_SAMPLE_MAX_ROWS: int = 10000
    DATA_VIEWER_GAP_THRESHOLD_MS: int = 0

    # Indicadores (ft_indicators; compartidos con ft_ta_lib)
    INDICATORS_DIR: pathlib.Path = pathlib.Path("./data/indicators")
    INDICATORS_MAX_CONCURRENT: int = Field(default=2, ge=1, le=16)

    # Recetas de datasets reales (ft_datasets)
    DATASETS_RECIPES_DIR: pathlib.Path = pathlib.Path("./datasets")
    DATASETS_DIR: pathlib.Path = pathlib.Path("./data/datasets")
    DATASETS_DOWNLOAD_TIMEOUT_S: int = Field(default=3600, ge=60)
    DATASETS_SETTLE_DAYS: int = Field(default=7, ge=1)

    @model_validator(mode="after")
    def _resolve_paths(self) -> FinancesSettings:
        root = self.PROJECT_ROOT.resolve()
        self.PROJECT_ROOT = root
        for name, value in self.__dict__.items():
            if isinstance(value, pathlib.Path) and not value.is_absolute():
                setattr(self, name, (root / value).resolve())
        return self

    def require_indicators_dir(self) -> pathlib.Path:
        """``INDICATORS_DIR`` validado (RF-7 de ``ft_indicators``).

        La validación se hace al usarlo (no al construir los settings) para que una
        configuración errónea de indicadores no impida usar el resto de herramientas.

        Raises:
            ConfigError: SI ``INDICATORS_DIR`` está dentro de ``PARQUET_DIR`` (los
                inventarios y ``ft_data_viewer`` lo confundirían con un exchange).
        """
        indicators = self.INDICATORS_DIR.resolve()
        parquet = self.PARQUET_DIR.resolve()
        if indicators.is_relative_to(parquet):
            msg = (
                f"FT_INDICATORS_DIR ({indicators}) no puede estar dentro de "
                f"FT_PARQUET_DIR ({parquet})"
            )
            raise ConfigError(msg)
        return indicators

    def require_datasets_dir(self) -> pathlib.Path:
        """``DATASETS_DIR`` validado (RF-11 de ``ft_datasets``).

        El almacén de recetas es una caché reconstruible: no se mezcla con las descargas
        del usuario. Se valida al usarlo, como :meth:`require_indicators_dir`.

        Raises:
            ConfigError: SI ``DATASETS_DIR`` está dentro de ``PARQUET_DIR``, lo contiene
                o es el mismo directorio.
        """
        datasets = self.DATASETS_DIR.resolve()
        parquet = self.PARQUET_DIR.resolve()
        if datasets.is_relative_to(parquet) or parquet.is_relative_to(datasets):
            msg = (
                f"FT_DATASETS_DIR ({datasets}) no puede anidar con "
                f"FT_PARQUET_DIR ({parquet}) en ningún sentido"
            )
            raise ConfigError(msg)
        return datasets


settings = FinancesSettings()
