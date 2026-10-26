"""Contratos de ``ft_datasets`` (plan §1): receta, *lock*, verificación y tarea.

La receta (``datasets/<nombre>.toml``) declara rangos cerrados y pasados; el *lock*
(``datasets/<nombre>.lock.json``) fija el hash de contenido canónico ``ftds-v1`` de
cada ítem. La regla de "rango asentado" depende del reloj y vive en
:func:`finances_core.datasets.recipes.validate_reproducible`, no en el modelo.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from finances_core.schemas.data_viewer import DatasetIdentifier
from finances_core.schemas.market_data import Timeframe
from finances_core.schemas.market_data_yfinance import INSTRUMENT_TYPE_MAP

#: Patrón de nombre de receta (RF-1); también impide rutas (``..``, ``/``, absolutas).
RECIPE_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
RecipeName = Annotated[str, StringConstraints(pattern=RECIPE_NAME_PATTERN)]
Exchange = Literal["binance", "yfinance"]

#: Algoritmo de hash de contenido canónico (plan §3.2).
HashAlgorithm = Literal["ftds-v1"]
HASH_ALGORITHM: Final[HashAlgorithm] = "ftds-v1"
#: Timeframes de Yahoo con historial ilimitado (RF-2: sin intradía de Yahoo).
YF_REPRODUCIBLE_TIMEFRAMES: tuple[str, ...] = ("1d", "1w", "1M")
#: ``market_type`` admitidos por exchange (RF-2). Yahoo solo escribe los tipos de
#: ``INSTRUMENT_TYPE_MAP`` (``other`` es el comodín de tipos desconocidos, no reproducible).
MARKET_TYPES_BY_EXCHANGE: dict[str, tuple[str, ...]] = {
    "binance": ("spot", "futures"),
    "yfinance": tuple(INSTRUMENT_TYPE_MAP.values()),
}
TIMEFRAMES_BY_EXCHANGE: dict[str, tuple[str, ...]] = {
    "binance": tuple(tf.value for tf in Timeframe),
    "yfinance": YF_REPRODUCIBLE_TIMEFRAMES,
}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FORBIDDEN_SYMBOL_CHARS = frozenset("/\\") | frozenset(" \t\r\n")
_EPOCH = date(1970, 1, 1)
_MS_PER_DAY = 86_400_000
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
Sha256 = Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]


def date_to_ms(value: date) -> int:
    """``date`` a ms epoch de su 00:00 UTC (aritmética entera exacta)."""
    return (value - _EPOCH).days * _MS_PER_DAY


def ms_to_date(ms: int) -> date:
    """Inverso de :func:`date_to_ms` (``ms`` debe caer en las 00:00 UTC)."""
    return _EPOCH + timedelta(days=ms // _MS_PER_DAY)


def _duplicates(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


class RecipeItem(BaseModel):
    """Un ítem de la receta: ``exchange × market_type × símbolo × timeframe × rango``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: Exchange
    market_type: str
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int  # exclusivo

    @property
    def key(self) -> str:
        """Clave estable ``{exchange}/{market_type}/{symbol}/{timeframe}@{start}..{end}``."""
        start = ms_to_date(self.start_ms).isoformat()
        end = ms_to_date(self.end_ms).isoformat()
        return f"{self.exchange}/{self.market_type}/{self.symbol}/{self.timeframe}@{start}..{end}"

    def dataset(self) -> DatasetIdentifier:
        """Identificador Hive del ítem (sin rango)."""
        return DatasetIdentifier.model_validate(
            {
                "exchange": self.exchange,
                "market_type": self.market_type,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
            }
        )


class RecipeBlock(BaseModel):
    """Bloque ``[[datasets]]``: producto cartesiano ``symbols × timeframes`` sobre un rango."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exchange: Exchange
    market_type: str
    symbols: list[str] = Field(min_length=1, max_length=100)
    timeframes: list[str] = Field(min_length=1)
    start: date  # 00:00 UTC
    end: date  # 00:00 UTC, exclusivo

    @field_validator("start", "end", mode="before")
    @classmethod
    def _strict_calendar_date(cls, value: Any) -> Any:
        """Solo fechas ``YYYY-MM-DD`` (TOML o texto ISO): nunca ``now``, *timestamps* ni horas."""
        is_date = isinstance(value, date) and not isinstance(value, datetime)
        is_iso_text = isinstance(value, str) and _ISO_DATE_RE.fullmatch(value) is not None
        if not (is_date or is_iso_text):
            msg = f"se espera una fecha YYYY-MM-DD, no {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("symbols")
    @classmethod
    def _valid_symbols(cls, symbols: list[str]) -> list[str]:
        for symbol in symbols:
            if not symbol or symbol in {".", ".."} or _FORBIDDEN_SYMBOL_CHARS & set(symbol):
                msg = f"symbol inválido {symbol!r} (vacío, '.'/'..', separadores o espacios)"
                raise ValueError(msg)
        dupes = _duplicates(symbols)
        if dupes:
            msg = f"symbols duplicados: {dupes}"
            raise ValueError(msg)
        return symbols

    @field_validator("timeframes")
    @classmethod
    def _unique_timeframes(cls, timeframes: list[str]) -> list[str]:
        dupes = _duplicates(timeframes)
        if dupes:
            msg = f"timeframes duplicados: {dupes}"
            raise ValueError(msg)
        return timeframes

    @model_validator(mode="after")
    def _per_exchange_rules(self) -> RecipeBlock:
        """RF-2: ``market_type`` y timeframes por exchange; ``start < end``."""
        allowed_markets = MARKET_TYPES_BY_EXCHANGE[self.exchange]
        if self.market_type not in allowed_markets:
            msg = (
                f"market_type {self.market_type!r} no admitido para {self.exchange} "
                f"({'|'.join(allowed_markets)})"
            )
            raise ValueError(msg)
        allowed_tfs = TIMEFRAMES_BY_EXCHANGE[self.exchange]
        invalid = [tf for tf in self.timeframes if tf not in allowed_tfs]
        if invalid:
            msg = (
                f"timeframes {invalid} no admitidos para {self.exchange} ({'|'.join(allowed_tfs)})"
            )
            raise ValueError(msg)
        if self.start >= self.end:
            msg = f"start ({self.start}) debe ser anterior a end ({self.end}, exclusivo)"
            raise ValueError(msg)
        return self

    def items(self) -> list[RecipeItem]:
        """Ítems del bloque en orden estable (símbolo, luego timeframe)."""
        start_ms, end_ms = date_to_ms(self.start), date_to_ms(self.end)
        return [
            RecipeItem(
                exchange=self.exchange,
                market_type=self.market_type,
                symbol=symbol,
                timeframe=timeframe,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            for symbol in self.symbols
            for timeframe in self.timeframes
        ]


class DatasetRecipe(BaseModel):
    """Receta versionada ``datasets/<nombre>.toml`` (RF-1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    name: RecipeName
    description: str = ""
    datasets: list[RecipeBlock] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_item_keys(self) -> DatasetRecipe:
        dupes = _duplicates([item.key for item in self.items()])
        if dupes:
            msg = f"ítems duplicados en la receta: {dupes}"
            raise ValueError(msg)
        return self

    def items(self) -> list[RecipeItem]:
        """Expansión cartesiana de todos los bloques, en orden estable (RF-1)."""
        return [item for block in self.datasets for item in block.items()]

    def canonical_sha256(self) -> str:
        """Hash canónico de la receta (plan §3.3) sobre la lista **ordenada** de claves.

        No depende de comentarios, espacios, orden de bloques ni ``description``.
        """
        keys = sorted(item.key for item in self.items())
        payload = json.dumps(keys, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


# --- Lock (plan §1.2) --------------------------------------------------------------------


class Probe(BaseModel):
    """Sonda de ``close`` en una fila fija (RF-6: clasificación ``SCALE_RESTATED``).

    ``close_bits``/``close`` son ``None`` si el valor es nulo; ``close`` también es
    ``None`` si no es finito (JSON no admite ``NaN``/``inf``). ``close`` es legible y no
    participa en comparaciones.
    """

    model_config = ConfigDict(extra="forbid")

    row: int = Field(ge=0)
    open_time_ms: int
    close_bits: Annotated[str, StringConstraints(pattern=r"^0x[0-9a-f]{8}$")] | None
    close: float | None


class ItemLock(BaseModel):
    """Hashes bloqueados de un ítem (``ftds-v1``)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    exchange: Exchange
    market_type: str
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    row_count: int = Field(ge=1)
    first_open_time_ms: int
    last_open_time_ms: int
    strict_columns: list[str]
    content_hash: Sha256
    column_hashes: dict[str, Sha256]
    month_hashes: dict[str, Sha256]
    informational: dict[str, Sha256]
    probes: list[Probe]
    missing_candles: int | None


class RecipeLock(BaseModel):
    """*Lock* ``datasets/<nombre>.lock.json`` (RF-7). Otro algoritmo o versión es inválido."""

    model_config = ConfigDict(extra="forbid")

    lock_version: Literal[1]
    hash_algorithm: HashAlgorithm
    recipe: RecipeName
    recipe_sha256: Sha256
    locked_at: datetime  # informativo
    tool_versions: dict[str, str]  # informativo
    items: list[ItemLock]  # orden = DatasetRecipe.items()


# --- Verificación y bloqueo (plan §1.3) ----------------------------------------------------

ItemStatus = Literal["OK", "OK_WITH_WARNINGS", "MISSING", "MISMATCH", "UNLOCKED"]
VerifyStatus = Literal["OK", "MISMATCH", "UNLOCKED", "RECIPE_CHANGED"]
Drift = Literal["ROWS_CHANGED", "SCALE_RESTATED", "VALUES_CHANGED"]
LockOutcome = Literal["WRITTEN", "UNCHANGED", "UPDATED", "REJECTED"]


class ItemVerification(BaseModel):
    key: str
    status: ItemStatus
    row_count: int
    expected_row_count: int | None
    content_hash: str | None
    expected_content_hash: str | None
    drift: Drift | None = None
    scale_factor: float | None = None  # solo SCALE_RESTATED
    columns_changed: list[str] = Field(default_factory=list)
    months_changed: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    action: str | None = None  # texto de RF-6


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


class VerifyReport(BaseModel):
    recipe: str
    status: VerifyStatus
    recipe_changed: bool
    items: list[ItemVerification]
    duration_ms: int

    def to_markdown(self) -> str:
        """Tabla Markdown (una fila por ítem) para ``$GITHUB_STEP_SUMMARY`` e issues."""
        lines = [
            f"### `ft_datasets verify {self.recipe}`: **{self.status}**",
            "",
        ]
        if self.recipe_changed:
            lines += ["> La receta cambió respecto al *lock* (`RECIPE_CHANGED`).", ""]
        lines += [
            "| Dataset | Estado | Filas | Deriva | Factor | Columnas | Meses |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in self.items:
            rows = (
                f"{item.row_count}"
                if item.expected_row_count is None
                else f"{item.row_count}/{item.expected_row_count}"
            )
            factor = "" if item.scale_factor is None else f"{item.scale_factor:g}"
            cells = [
                f"`{item.key}`",
                item.status,
                rows,
                item.drift or "",
                factor,
                ", ".join(item.columns_changed),
                ", ".join(item.months_changed),
            ]
            lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
        actions = sorted({item.action for item in self.items if item.action})
        warnings = [f"`{item.key}`: {w}" for item in self.items for w in item.warnings]
        if actions or warnings:
            lines.append("")
        lines += [f"- {_md_cell(a)}" for a in actions]
        lines += [f"- ⚠ {_md_cell(w)}" for w in warnings]
        return "\n".join(lines) + "\n"


class LockResult(BaseModel):
    recipe: str
    outcome: LockOutcome
    lock_path: str
    differences: list[str]  # claves/campos que difieren del lock previo


# --- Tarea de materialización (plan §1.3) --------------------------------------------------

MaterializePhase = Literal[
    "PENDING", "VALIDATING", "PRECHECKING", "DOWNLOADING", "VERIFYING", "COMPLETED", "FAILED"
]
#: Orden de fases no terminales (RF-8), para ``TaskRecorder``.
MATERIALIZE_PHASE_ORDER: tuple[str, ...] = (
    "PENDING",
    "VALIDATING",
    "PRECHECKING",
    "DOWNLOADING",
    "VERIFYING",
)
MaterializeOutcome = VerifyStatus | Literal["NOT_VERIFIED", "DOWNLOAD_FAILED"]


class DownloadBlock(BaseModel):
    """Una invocación de la CLI de un descargador."""

    exchange: Exchange
    market_type: str
    symbols: list[str]
    timeframes: list[str]
    start_ms: int
    end_ms_inclusive: int  # end_ms - 1
    argv: list[str]  # comando exacto (sin rutas absolutas del ejecutable)


class MaterializePlan(BaseModel):
    task_id: str
    created_at_ms: int
    recipe: str
    skipped_items: list[str]  # OK en la pre-verificación
    refetch_items: list[str]
    blocks: list[DownloadBlock]


class BlockRun(BaseModel):
    block: int
    exit_code: int | None  # None = no se pudo lanzar / timeout
    duration_ms: int
    timed_out: bool
    stderr_tail: list[str]  # últimas 50 líneas
    downloader_task_id: str | None


class MaterializeProgress(BaseModel):
    task_id: str
    status: MaterializePhase
    recipe: str
    items_total: int
    items_skipped: int
    blocks_total: int
    blocks_done: int
    errors: list[str]


class MaterializeResult(BaseModel):
    task_id: str
    status: Literal["COMPLETED", "FAILED"]
    outcome: MaterializeOutcome
    recipe: str
    blocks: list[BlockRun]
    verify: VerifyReport | None
    errors: list[str]


__all__ = [
    "HASH_ALGORITHM",
    "HashAlgorithm",
    "MARKET_TYPES_BY_EXCHANGE",
    "MATERIALIZE_PHASE_ORDER",
    "RECIPE_NAME_PATTERN",
    "TIMEFRAMES_BY_EXCHANGE",
    "YF_REPRODUCIBLE_TIMEFRAMES",
    "BlockRun",
    "DatasetRecipe",
    "DownloadBlock",
    "Drift",
    "Exchange",
    "ItemLock",
    "ItemStatus",
    "ItemVerification",
    "LockOutcome",
    "LockResult",
    "MaterializeOutcome",
    "MaterializePhase",
    "MaterializePlan",
    "MaterializeProgress",
    "MaterializeResult",
    "Probe",
    "RecipeBlock",
    "RecipeItem",
    "RecipeLock",
    "RecipeName",
    "VerifyReport",
    "VerifyStatus",
    "date_to_ms",
    "ms_to_date",
]
