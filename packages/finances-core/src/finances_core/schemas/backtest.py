"""Contratos Pydantic del backtesting compartido (specs/finances_backtester/plan.md §1.2).

Una única forma canónica de petición (:class:`BacktestRequest`) para CLI y API (RF-3), la
estrategia resuelta con la identidad de su código (RF-19), la ficha de registro
(:class:`KindInfo`, RF-1), los costes (RF-5), el manifiesto y el resumen de una corrida
(RF-7, RF-9), las páginas de lectura y la respuesta de despacho asíncrono (Art. 18).

Frontera de seguridad (RF-18, ADR-B10): ningún modelo de petición acepta código, rutas,
nombres de módulo ni directorios de estrategias; ``extra="forbid"`` rechaza cualquier
clave no declarada. Los importes se validan como ``float`` de Python y se almacenan como
``Float32`` en las tablas canónicas (RF-6, RF-11).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.strategy import HASH_PATTERN

#: Identificador de estrategia (RF-16): igual al nombre de su carpeta.
KIND_PATTERN = r"^[a-z][a-z0-9_]{2,39}$"
#: UUID v7 en minúsculas, como los genera ``ft_strategy_manager`` (RF-3).
UUID7_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

StrategyOrigin = Literal["builtin", "local"]
ParamScalar = int | float | bool | str


#: Caracteres que convierten un componente en una ruta (separadores y unidad de Windows).
_PATH_CHARS = frozenset("/\\:")
_FIRST_PRINTABLE = 0x20
_DEL = 0x7F


def check_path_component(value: str) -> str:
    """Valida que ``value`` sea un único segmento de ruta inerte (RF-18).

    Rechaza el vacío, ``.``/``..``, los separadores ``/`` y ``\\``, las formas de unidad o
    absolutas (``C:``, ``\\\\server``) y los caracteres de control.

    Raises:
        ValueError: SI ``value`` no es un segmento seguro.
    """
    if value in {"", ".", ".."}:
        msg = f"componente de ruta inválido: {value!r}"
        raise ValueError(msg)
    if any(c in _PATH_CHARS or ord(c) < _FIRST_PRINTABLE or ord(c) == _DEL for c in value):
        msg = f"componente de ruta con separador, unidad o carácter de control: {value!r}"
        raise ValueError(msg)
    return value


class BacktestDataset(DatasetIdentifier):
    """``DatasetIdentifier`` de una petición: rechaza claves extra y *path traversal* (RF-18).

    Cada componente se usa como segmento del layout Hive bajo ``FT_PARQUET_DIR``; uno con
    ``..``, separadores, unidad o caracteres de control podría salir de ese directorio.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("exchange", "market_type", "symbol", "timeframe")
    @classmethod
    def _check_path_component(cls, value: str) -> str:
        return check_path_component(value)


INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class BacktestRequest(BaseModel):
    """Petición canónica de backtest (RF-3); ``--request-file`` acepta el mismo cuerpo."""

    model_config = ConfigDict(extra="forbid")

    dataset: BacktestDataset
    strategy_id: str = Field(pattern=UUID7_PATTERN)
    # None => última versión (ft_strategy_manager RF-2).
    strategy_version: int | None = Field(default=None, ge=1, strict=True)
    # Rango inclusivo sobre open_time_ms (Int64, ms UTC); None => todo el dataset.
    start_ms: int | None = Field(default=None, ge=INT64_MIN, le=INT64_MAX, strict=True)
    end_ms: int | None = Field(default=None, ge=INT64_MIN, le=INT64_MAX, strict=True)
    initial_cash: float = Field(default=10_000.0, gt=0, allow_inf_nan=False)
    size_fraction: float = Field(default=1.0, gt=0, le=1)
    fees_bps: float = Field(default=10.0, ge=0, allow_inf_nan=False)
    slippage_bps: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    force: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _check_range(self) -> BacktestRequest:
        if self.start_ms is not None and self.end_ms is not None and self.start_ms > self.end_ms:
            msg = f"start_ms ({self.start_ms}) debe ser <= end_ms ({self.end_ms})"
            raise ValueError(msg)
        return self


class ResolvedStrategy(BaseModel):
    """Estrategia leída de ``ft_strategy_manager`` y validada contra el registro (RF-1, RF-19)."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    name: str
    version: int = Field(ge=1)
    hash: str = Field(pattern=HASH_PATTERN)  # SHA-256 de ft_strategy_manager
    kind: str = Field(pattern=KIND_PATTERN)
    params: dict[str, ParamScalar]  # ya validados por el Params de la estrategia
    kind_version: str  # BaseStrategy.version
    code_hash: str = Field(pattern=HASH_PATTERN)  # strategy_code_hash (RF-19)
    origin: StrategyOrigin


class KindInfo(BaseModel):
    """Ficha de una estrategia registrada (``kinds`` / ``GET /kinds``, RF-1).

    Las carpetas locales inválidas aparecen con ``valid = False`` y su ``error``.
    """

    kind: str
    version: str | None
    origin: StrategyOrigin
    description: str | None
    code_hash: str | None
    # Params.model_json_schema(): fuente única, no existe schema.json (RF-1).
    params_schema: dict[str, Any] | None
    default_params: dict[str, Any] | None
    metrics_target: dict[str, str] | None
    valid: bool
    error: str | None


class CostModel(BaseModel):
    """Costes de ejecución en puntos básicos (RF-5); las tasas son ``bps / 10 000``."""

    model_config = ConfigDict(extra="forbid")

    fees_bps: float = Field(ge=0, allow_inf_nan=False)
    slippage_bps: float = Field(ge=0, allow_inf_nan=False)


class RunSummary(BaseModel):
    """``summary.json`` de una corrida (RF-7); valores serializados desde ``Float32``."""

    total_return: float
    # None si la anualización no es finita (deriva de RF-7).
    cagr: float | None
    max_drawdown: float
    sharpe: float | None
    sortino: float | None
    win_rate: float | None
    n_trades: int
    exposure: float
    initial_cash: float
    final_equity: float
    n_bars: int
    # Rango efectivo y advertencias de RF-7 (``insufficient_history``, ``data_gaps``,
    # ``cagr_not_finite``). ``metrics.compute`` no los conoce: ``results_store.write_run``
    # los copia del manifiesto, que es su única fuente.
    start_ms: int | None = None
    end_ms: int | None = None
    warnings: list[str] = Field(default_factory=list)


class RunManifest(BaseModel):
    """``manifest.json``: marcador de completitud de una corrida (RF-9)."""

    run_id: str
    engine: str
    engine_version: str
    catalog_version: str
    result_schema_version: str
    request: BacktestRequest
    strategy: ResolvedStrategy
    start_ms: int
    end_ms: int
    n_bars: int
    data_hash: str
    data_gaps: int
    created_at_ms: int
    duration_ms: int
    warnings: list[str]
    python_version: str
    libraries: dict[str, str]

    # Campos de identidad de la estrategia con los nombres de RF-9/RF-13, derivados de
    # ``strategy`` (única fuente): se serializan en ``manifest.json`` y se ignoran al leer.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def kind_version(self) -> str:
        return self.strategy.kind_version

    @computed_field  # type: ignore[prop-decorator]
    @property
    def strategy_code_hash(self) -> str:
        return self.strategy.code_hash

    @computed_field  # type: ignore[prop-decorator]
    @property
    def strategy_origin(self) -> StrategyOrigin:
        return self.strategy.origin


class BacktestDispatchResponse(BaseModel):
    """Respuesta 202 del despacho asíncrono (Art. 18)."""

    task_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"
    poll_url: str
    result_url: str


class BacktestTaskResult(BaseModel):
    """Resultado de una tarea terminada (``GET /result/{task_id}``)."""

    run_id: str
    cached: bool
    summary: RunSummary
    manifest: RunManifest


class RunListItem(BaseModel):
    """Fila del listado de corridas almacenadas (RF-9)."""

    run_id: str
    engine: str
    strategy_id: str
    strategy_version: int
    kind: str
    dataset: DatasetIdentifier
    created_at_ms: int
    summary: RunSummary


class RunsPage(BaseModel):
    """Página de corridas con paginación por cursor (RF-9)."""

    items: list[RunListItem]
    count: int
    next_cursor: str | None


class TradesPage(BaseModel):
    """Página de ``trades`` paginada por ``entry_time_ms`` (RF-9)."""

    items: list[dict[str, Any]]
    count: int
    next_after_ms: int | None


class EquityPage(BaseModel):
    """Página de ``equity`` paginada por ``open_time_ms`` (RF-9)."""

    items: list[dict[str, Any]]
    count: int
    next_after_ms: int | None
