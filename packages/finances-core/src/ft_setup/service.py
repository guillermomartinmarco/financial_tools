"""Lógica compartida de ``ft_setup`` (Art. 1: CLI y API son adaptadores delgados).

Contiene el diagnóstico de variables (RF-2/RF-3/RF-4) y la inicialización del
entorno (RF-1/RF-7). No importa Typer ni FastAPI.
"""

from __future__ import annotations

import json
import os
import socket
import tomllib
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable
from importlib import resources
from pathlib import Path
from typing import Any

import structlog
import uuid6
from dotenv import dotenv_values

from finances_core.config import ROOT_MARKER, FinancesSettings
from finances_core.schemas.setup import (
    EnvVarDiagnosis,
    EnvVarSpec,
    InitTaskResult,
    SetupDoctorResponse,
    VarSource,
)
from finances_core.tasks import SQLTaskManager, now_ms

logger = structlog.get_logger(__name__)

#: Patrones que fuerzan el enmascaramiento (RF-3), comparados en mayúsculas.
SECRET_PATTERNS: tuple[str, ...] = (
    "KEY",
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "PRIVATE",
    "MNEMONIC",
    "API_KEY",
    "ACCESS_KEY",
)

ENV_LIST_FILENAME = "env_list.conf"
ENV_TEMPLATE_FILENAME = ".env.template"
_DIRS_ATTRS: tuple[str, ...] = (
    "DATA_DIR",
    "RAW_DIR",
    "PROCESSED_DIR",
    "PARQUET_DIR",
    "INDICATORS_DIR",
    "LOGS_DIR",
    "STRATEGIES_DIR",
    "RESULTS_DIR",
    "SHARE_DIR",
)


# --------------------------------------------------------------------------- specs
def package_dir() -> Path:
    """Directorio instalado del paquete ``ft_setup``."""
    return Path(str(resources.files("ft_setup")))


def _template(name: str) -> Path:
    return package_dir() / "templates" / name


def load_env_specs(source: Path | None = None) -> list[EnvVarSpec]:
    """Carga ``env_list.conf`` (fuente canónica) y devuelve los specs tipados."""
    if source is None:
        candidates = [package_dir() / ENV_LIST_FILENAME, _template("env_list.conf.j2")]
        path = next((c for c in candidates if c.exists()), None)
        if path is None:  # pragma: no cover - instalación corrupta
            raise EnvListNotFoundError()
    else:
        path = source

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [EnvVarSpec.model_validate(item) for item in data.get("vars", [])]


# ------------------------------------------------------------------------ secrets
def detect_secret(name: str, spec_secret: bool = False) -> bool:
    """RF-3: ``True`` si el flag explícito o el nombre indican secreto."""
    if spec_secret:
        return True
    upper = name.upper()
    return any(pattern in upper for pattern in SECRET_PATTERNS)


def mask_value(value: str | None, secret: bool) -> str:
    """Enmascara secretos; ``<unset>`` cuando no hay valor."""
    if not value:
        return "<unset>"
    return "***" if secret else value


# --------------------------------------------------------------------- validation
MIN_PORT = 1
MAX_PORT = 65535
BOOL_VALUES = frozenset({"1", "0", "true", "false", "yes", "no"})


class EnvListNotFoundError(RuntimeError):
    """``env_list.conf`` no disponible en la instalación."""

    def __init__(self) -> None:
        super().__init__("No se encontró env_list.conf ni env_list.conf.j2")


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _validate_port(value: str) -> str | None:
    try:
        port = int(value)
    except ValueError:
        return f"puerto no numérico: {value!r}"
    if not MIN_PORT <= port <= MAX_PORT:
        return f"puerto fuera de rango [{MIN_PORT}, {MAX_PORT}]: {port}"
    return None


def _validate_host(value: str) -> str | None:
    host = value.strip()
    if not host:
        return "host vacío"
    try:
        socket.inet_aton(host)
    except OSError:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_")
        if set(host) <= allowed and not host.startswith(("-", ".")):
            return None
        return f"host inválido: {value!r}"
    else:
        return None


def _validate_int(value: str) -> str | None:
    try:
        int(value)
    except ValueError:
        return f"se esperaba int, recibido {value!r}"
    return None


def _validate_float(value: str) -> str | None:
    try:
        float(value)
    except ValueError:
        return f"se esperaba float, recibido {value!r}"
    return None


def _validate_bool(value: str) -> str | None:
    if value.strip().lower() not in BOOL_VALUES:
        return f"se esperaba bool, recibido {value!r}"
    return None


def _validate_url(value: str) -> str | None:
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return f"URL no parseable: {value!r}"
    return None


def _validate_path(value: str) -> str | None:
    path = Path(value).expanduser()
    if path.exists():
        return None
    anchor = _nearest_existing(path)
    if anchor.is_dir() and os.access(anchor, os.W_OK):
        return None
    return f"ruta no existente ni creable: {value!r}"


_TYPE_VALIDATORS: dict[str, Callable[[str], str | None]] = {
    "int": _validate_int,
    "float": _validate_float,
    "bool": _validate_bool,
    "url": _validate_url,
    "path": _validate_path,
    "host_port": _validate_path,
    "str": lambda _value: None,
}


def validate_var(spec: EnvVarSpec, value: str | None) -> tuple[bool, str | None]:
    """RF-4: valida tipo y rango de una variable. Devuelve ``(valid, error)``."""
    if value is None or value == "":
        if spec.required:
            return False, f"variable requerida ausente: {spec.name}"
        return True, None

    upper_name = spec.name.upper()
    if upper_name.endswith("_PORT"):
        validator: Callable[[str], str | None] = _validate_port
    elif upper_name.endswith("_HOST"):
        validator = _validate_host
    else:
        validator = _TYPE_VALIDATORS.get(spec.var_type, _TYPE_VALIDATORS["str"])

    error = validator(value)
    return error is None, error


# ---------------------------------------------------------------------- diagnosis
def _dotenv_values(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        return {}
    return {k: v for k, v in dotenv_values(env_file).items() if v is not None}


def _resolve_source(name: str, raw_value: str | None, dotenv: dict[str, str]) -> VarSource:
    if name in os.environ:
        return VarSource.ENV
    if name in dotenv:
        return VarSource.DOTENV
    if raw_value is not None:
        return VarSource.DEFAULT
    return VarSource.MISSING


def _raw_setting(settings: FinancesSettings, name: str) -> str | None:
    attr = name.removeprefix("FT_")
    if not hasattr(settings, attr):
        return None
    value = getattr(settings, attr)
    if value is None:
        return None
    return str(value)


def diagnose(
    specs: Iterable[EnvVarSpec] | None = None,
    settings: FinancesSettings | None = None,
    env_file: Path | None = None,
) -> SetupDoctorResponse:
    """RF-2: diagnóstico completo de variables frente a ``env_list.conf``."""
    spec_list = list(specs) if specs is not None else load_env_specs()
    effective_settings = settings if settings is not None else FinancesSettings()
    dotenv = _dotenv_values(env_file or Path(".env"))

    diagnoses: list[EnvVarDiagnosis] = []
    for spec in spec_list:
        raw_value = _raw_setting(effective_settings, spec.name)
        if raw_value is None:
            raw_value = os.environ.get(spec.name)
        source = _resolve_source(spec.name, raw_value, dotenv)
        secret = detect_secret(spec.name, spec.secret)
        valid, error = validate_var(spec, raw_value)

        diagnoses.append(
            EnvVarDiagnosis(
                spec=spec,
                value=None if secret else raw_value,
                masked_value=mask_value(raw_value, secret),
                source=source,
                valid=valid,
                validation_error=error,
            )
        )

    summary = _summarise(diagnoses)
    ok = all(d.valid for d in diagnoses if d.spec.required)
    return SetupDoctorResponse(ok=ok, timestamp_ms=now_ms(), vars=diagnoses, summary=summary)


def _summarise(diagnoses: list[EnvVarDiagnosis]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for diagnosis in diagnoses:
        counter["total"] += 1
        if diagnosis.spec.required:
            counter["required_ok" if diagnosis.valid else "required_missing"] += 1
        elif diagnosis.source is VarSource.MISSING:
            counter["optional_unset"] += 1
        else:
            counter["optional_set"] += 1
        if not diagnosis.valid:
            counter["invalid"] += 1
    return {
        "total": counter["total"],
        "required_ok": counter["required_ok"],
        "required_missing": counter["required_missing"],
        "optional_set": counter["optional_set"],
        "optional_unset": counter["optional_unset"],
        "invalid": counter["invalid"],
    }


def has_missing_required(response: SetupDoctorResponse) -> bool:
    return any(
        not d.valid and d.source is VarSource.MISSING for d in response.vars if d.spec.required
    )


# --------------------------------------------------------------------------- init
def _render_template(path: Path, replacements: dict[str, str]) -> str:
    """Sustitución mínima de ``{{ clave }}`` (sin dependencia de Jinja2)."""
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{ " + key + " }}", value).replace("{{" + key + "}}", value)
    return text


def _env_template_sections(specs: Iterable[EnvVarSpec]) -> str:
    grouped: dict[str, list[EnvVarSpec]] = {}
    for spec in specs:
        grouped.setdefault(spec.source_tool or "ft_setup", []).append(spec)

    blocks: list[str] = []
    for tool, tool_specs in grouped.items():
        lines = [
            "# ============================================================",
            f"# {tool}",
            "# ============================================================",
        ]
        for spec in tool_specs:
            required = "true" if spec.required else "false"
            lines.append(f"# {spec.description} | requerido: {required}")
            if spec.example:
                lines.append(f"# ejemplo: {spec.example}")
            secret = detect_secret(spec.name, spec.secret)
            if secret:
                lines.append("# RELLENAR (secreto enmascarado en logs)")
                lines.append(f'{spec.name}=""')
            else:
                lines.append(f'{spec.name}="{spec.example or ""}"')
            lines.append("")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def build_env_template(specs: Iterable[EnvVarSpec]) -> str:
    """Renderiza ``.env.template`` a partir de los specs canónicos."""
    spec_list = list(specs)
    header_template = _template(".env.template.j2")
    if header_template.exists():
        return _render_template(header_template, {"sections": _env_template_sections(spec_list)})
    return _env_template_sections(spec_list)  # pragma: no cover


def start_init_task(force: bool = False, dry_run: bool = False) -> InitTaskResult:
    """Crea el registro PENDING de una inicialización (para el modo API)."""
    task_id = str(uuid6.uuid7())
    manager = SQLTaskManager()
    manager.create_task(task_id, status="PENDING", phase="INIT")
    logger.info("init_task_created", task_id=task_id, force=force, dry_run=dry_run)
    return InitTaskResult(task_id=task_id, status="PENDING", created_at_ms=now_ms())


def run_init(
    force: bool = False,
    dry_run: bool = False,
    task_id: str | None = None,
    project_root: Path | None = None,
    manager: SQLTaskManager | None = None,
) -> InitTaskResult:
    """RF-1: inicializa directorios, ``.env.template`` y ``env_list.conf``.

    Idempotente (Art. 12): re-ejecutar no duplica ni corrompe. ``dry_run`` no
    escribe en disco. Cada ejecución queda registrada en ``tasks.db`` (RF-7).
    """
    settings = FinancesSettings(PROJECT_ROOT=project_root) if project_root else FinancesSettings()
    root = settings.PROJECT_ROOT
    task_manager = manager if manager is not None else SQLTaskManager(settings.TASKS_DB_PATH)
    identifier = task_id or str(uuid6.uuid7())

    if task_manager.get_task(identifier) is None:
        task_manager.create_task(identifier, status="PENDING", phase="INIT")
    task_manager.update_task(identifier, status="RUNNING")
    logger.info("init_started", task_id=identifier, root=str(root), dry_run=dry_run)

    created_dirs: list[str] = []
    generated_files: list[str] = []
    errors: list[str] = []

    try:
        for attr in _DIRS_ATTRS:
            directory = Path(getattr(settings, attr))
            if not dry_run:
                directory.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(directory))

        marker = root / ROOT_MARKER
        if not marker.exists():
            generated_files.append(str(marker))
            if not dry_run:
                marker.write_text(
                    "# Marca la raíz del proyecto financial_tools (Art. 2)\n", encoding="utf-8"
                )

        specs = load_env_specs()
        env_template_path = root / ENV_TEMPLATE_FILENAME
        if force or not env_template_path.exists():
            generated_files.append(str(env_template_path))
            if not dry_run:
                env_template_path.write_text(build_env_template(specs), encoding="utf-8")

        env_list_target = package_dir() / ENV_LIST_FILENAME
        writable = os.access(env_list_target.parent, os.W_OK)
        if (force or not env_list_target.exists()) and writable:
            generated_files.append(str(env_list_target))
            if not dry_run:
                env_list_target.write_text(
                    _template("env_list.conf.j2").read_text(encoding="utf-8"),
                    encoding="utf-8",
                    newline="\n",  # LF en cualquier SO (.gitattributes eol=lf)
                )
        elif not env_list_target.exists():  # pragma: no cover - instalación read-only
            logger.warning("env_list_not_writable", path=str(env_list_target), root=str(root))

        if not dry_run:
            task_manager.ensure_ready()
    except Exception as exc:  # noqa: BLE001 - se persiste y se propaga
        errors.append(str(exc))
        task_manager.update_task(identifier, status="FAILED", error=str(exc), errors_json=errors)
        logger.error("init_failed", task_id=identifier, error=str(exc))  # noqa: TRY400
        raise

    summary = {
        "root": str(root),
        "created_dirs": created_dirs,
        "generated_files": generated_files,
        "dry_run": dry_run,
        "ok": True,
    }
    task_manager.update_task(
        identifier,
        status="COMPLETED",
        result_summary_json=summary,
        errors_json=errors,
    )
    record = task_manager.get_task(identifier) or {}
    logger.info(
        "init_completed",
        task_id=identifier,
        dirs=len(created_dirs),
        files=len(generated_files),
    )
    return InitTaskResult(
        task_id=identifier,
        status="COMPLETED",
        created_at_ms=int(record.get("created_at_ms") or now_ms()),
        started_at_ms=record.get("started_at_ms"),
        finished_at_ms=record.get("finished_at_ms"),
        created_dirs=created_dirs,
        generated_files=generated_files,
        errors=errors,
    )


def get_init_result(task_id: str, manager: SQLTaskManager | None = None) -> InitTaskResult | None:
    """Recupera el estado persistido de una inicialización (RF-5)."""
    task_manager = manager if manager is not None else SQLTaskManager()
    record = task_manager.get_task(task_id)
    if record is None:
        return None

    summary: dict[str, Any]
    try:
        summary = json.loads(record.get("result_summary_json") or "{}")
    except ValueError:  # pragma: no cover - JSON corrupto
        summary = {}
    try:
        errors = json.loads(record.get("errors_json") or "[]")
    except ValueError:  # pragma: no cover
        errors = [record.get("error")] if record.get("error") else []

    return InitTaskResult(
        task_id=str(record["task_id"]),
        status=record["status"],
        created_at_ms=int(record.get("created_at_ms") or 0),
        started_at_ms=record.get("started_at_ms"),
        finished_at_ms=record.get("finished_at_ms"),
        created_dirs=list(summary.get("created_dirs", [])),
        generated_files=list(summary.get("generated_files", [])),
        errors=list(errors),
    )
