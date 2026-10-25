from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class VarSource(StrEnum):
    """Origen efectivo del valor de una variable de entorno."""

    ENV = "env"
    DOTENV = ".env"
    DEFAULT = "default"
    MISSING = "missing"


class SecretLevel(StrEnum):
    PLAIN = "plain"
    MASKED = "masked"


class EnvVarSpec(BaseModel):
    """Definición canónica de una variable FT_* esperada por la suite."""

    name: str = Field(pattern=r"^FT_[A-Z0-9_]+$")
    description: str
    example: str | None = None
    required: bool = False
    var_type: Literal["str", "int", "float", "bool", "path", "url", "host_port"] = "str"
    default: str | None = None
    secret: bool = False
    source_tool: str | None = None
    since_version: str | None = None


class EnvVarDiagnosis(BaseModel):
    """Resultado de diagnóstico para una variable."""

    spec: EnvVarSpec
    value: str | None = None
    masked_value: str
    source: VarSource
    valid: bool = True
    validation_error: str | None = None


class SetupDoctorResponse(BaseModel):
    """Respuesta unificada CLI/API para diagnóstico."""

    ok: bool
    timestamp_ms: int
    vars: list[EnvVarDiagnosis]
    summary: dict[str, int]


class InitTaskResult(BaseModel):
    """Resultado de tarea de inicialización (persistido en tasks.db)."""

    task_id: str
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    created_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    created_dirs: list[str] = []
    generated_files: list[str] = []
    errors: list[str] = []
