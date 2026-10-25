# Plan — `ft_setup` (MVP v1.0)

## 1. Modelo de Datos y Contratos (Pydantic v2)

### 1.1 Esquema Canónico de Variable de Entorno (`packages/finances-core/schemas/setup.py`)

```python
from enum import Enum
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal

class VarSource(str, Enum):
    ENV = "env"
    DOTENV = ".env"
    DEFAULT = "default"
    MISSING = "missing"

class SecretLevel(str, Enum):
    PLAIN = "plain"
    MASKED = "masked"

class EnvVarSpec(BaseModel):
    """Definición canónica de una variable FT_* esperada por la suite."""
    name: str = Field(pattern=r"^FT_[A-Z0-9_]+$")
    description: str
    example: Optional[str] = None
    required: bool = False
    var_type: Literal["str", "int", "float", "bool", "path", "url", "host_port"] = "str"
    default: Optional[str] = None
    secret: bool = False  # True si nombre matchea patrones secretos

class EnvVarDiagnosis(BaseModel):
    """Resultado de diagnóstico para una variable."""
    spec: EnvVarSpec
    value: Optional[str] = None           # Valor real (NUNCA se serializa en API si secret=True)
    masked_value: str                     # Valor para mostrar: "***" si secret, else value o "<unset>"
    source: VarSource
    valid: bool = True
    validation_error: Optional[str] = None

class SetupDoctorResponse(BaseModel):
    """Respuesta unificada CLI/API para diagnóstico."""
    ok: bool
    timestamp_ms: int                     # int64 UTC ms (Art. 5 Constitución)
    vars: list[EnvVarDiagnosis]
    summary: dict                         # {total, required_ok, required_missing, optional_set, optional_unset, invalid}

class InitTaskResult(BaseModel):
    """Resultado de tarea de inicialización (persistido en tasks.db)."""
    task_id: str                          # UUID v7
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    created_at_ms: int
    started_at_ms: Optional[int] = None
    finished_at_ms: Optional[int] = None
    created_dirs: list[str] = []
    generated_files: list[str] = []
    errors: list[str] = []
```

### 1.2 Configuración Central (`packages/finances-core/src/finances_core/config.py`)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import pathlib

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
    LOG_FORMAT: Literal["json", "rich"] = "json"  # API usa json, CLI usa rich

    # Secrets (ejemplos - se enmascaran automáticamente por nombre)
    BINANCE_API_KEY: Optional[str] = None
    BINANCE_API_SECRET: Optional[str] = None
    YFINANCE_API_KEY: Optional[str] = None
    # ... más según suite
```

### 1.3 `env_list.conf` Formato (TOML, fuente de verdad para RF-2)

```toml
# packages/finances-core/src/ft_setup/env_list.conf
[[vars]]
name = "FT_DATA_DIR"
description = "Directorio raíz de datos de mercado"
example = "./data"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_RAW_DIR"
description = "Directorio de datos crudos descargados"
example = "./data/raw"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_PROCESSED_DIR"
description = "Directorio de datos procesados/normalizados"
example = "./data/processed"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_PARQUET_DIR"
description = "Directorio de archivos Parquet particionados"
example = "./data/parquet"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_LOGS_DIR"
description = "Directorio de logs de la suite"
example = "./logs"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_STRATEGIES_DIR"
description = "Directorio de definiciones de estrategias"
example = "./strategies"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_RESULTS_DIR"
description = "Directorio de resultados de backtests/análisis"
example = "./results"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_SHARE_DIR"
description = "Directorio compartido (tasks.db, etc.)"
example = "./share"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_TASKS_DB_PATH"
description = "Ruta absoluta al archivo SQLite de tareas"
example = "./share/tasks.db"
required = true
var_type = "path"
secret = false

[[vars]]
name = "FT_API_HOST"
description = "Host del servidor FastAPI"
example = "0.0.0.0"
required = true
var_type = "host_port"
secret = false

[[vars]]
name = "FT_API_PORT"
description = "Puerto del servidor FastAPI"
example = "8000"
required = true
var_type = "host_port"
secret = false

[[vars]]
name = "FT_API_WORKERS"
description = "Número de workers uvicorn"
example = "1"
required = false
var_type = "int"
secret = false

[[vars]]
name = "FT_LOG_LEVEL"
description = "Nivel de logging (DEBUG, INFO, WARNING, ERROR)"
example = "INFO"
required = false
var_type = "str"
secret = false

[[vars]]
name = "FT_BINANCE_API_KEY"
description = "API Key de Binance (Spot/Futures)"
example = "abc123..."
required = false
var_type = "str"
secret = true

[[vars]]
name = "FT_BINANCE_API_SECRET"
description = "API Secret de Binance"
example = "xyz789..."
required = false
var_type = "str"
secret = true

[[vars]]
name = "FT_YFINANCE_API_KEY"
description = "API Key de Yahoo Finance (si aplica)"
example = "key..."
required = false
var_type = "str"
secret = true
```

---

## 2. Mapeo de Interfaces

### 2.1 CLI (Typer) — `packages/finances-core/src/ft_setup/cli.py`

```python
app = typer.Typer(
    name="ft_setup",
    help="Inicialización y diagnóstico del entorno financial_tools",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Sobrescribir archivos existentes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Mostrar qué se haría sin ejecutar"),
) -> None:
    """Inicializa estructura de directorios, .env.template y env_list.conf."""

@app.command()
def doctor(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Nivel DEBUG"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo WARNING+"),
    format: Literal["table", "json"] = typer.Option("table", "--format", help="Formato de salida"),
) -> None:
    """Diagnostica variables de entorno FT_* frente a env_list.conf."""

@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    log_format: Literal["rich", "json"] = typer.Option("rich", "--log-format"),
) -> None:
    """Configura logging dual (Art. 9 Constitución)."""
```

### 2.2 API (FastAPI) — `packages/finances-core/src/ft_setup/api.py`

```python
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/setup", tags=["setup"])

@router.get("/doctor", response_model=SetupDoctorResponse)
async def doctor_endpoint() -> SetupDoctorResponse:
    """Diagnóstico completo de variables de entorno (JSON)."""

@router.post("/init", status_code=status.HTTP_202_ACCEPTED)
async def init_endpoint(
    background_tasks: BackgroundTasks,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Inicializa entorno en background.
    Retorna: {"task_id": "<uuid-v7>", "status": "ACCEPTED", "poll_url": "/setup/status/<task_id>"}
    """

@router.get("/status/{task_id}", response_model=InitTaskResult)
async def init_status(task_id: str) -> InitTaskResult:
    """Polling de estado de tarea de inicialización."""
```

---

## 3. Algoritmos Clave

### 3.1 `doctor` (CLI + API compartido)

```
FUNCTION diagnose() -> SetupDoctorResponse:
    1. Load env_list.conf → List[EnvVarSpec]
    2. Load FinancesSettings() (Pydantic Settings resuelve prioridad: ENV > .env > default)
    3. FOR each spec IN specs:
         a. raw_value = getattr(settings, spec.name) if exists else None
         b. source = determine_source(spec.name, raw_value)
         c. is_secret = spec.secret OR matches_secret_pattern(spec.name)
         d. masked = "***" if is_secret AND raw_value ELSE (raw_value OR "<unset>")
         e. valid, error = validate_type_and_range(spec, raw_value)
         f. append EnvVarDiagnosis(...)
    4. summary = compute_summary(diagnoses)
    5. ok = all(d.valid for d in diagnoses if d.spec.required)
    6. RETURN SetupDoctorResponse(ok, now_ms(), diagnoses, summary)
```

### 3.2 `init` (Background Task)

```
FUNCTION run_init(force: bool, dry_run: bool) -> InitTaskResult:
    1. task_id = uuid7()
    2. persist_task(task_id, status=PENDING)
    3. UPDATE status=RUNNING, started_at=now_ms()
    4. dirs_to_create = [settings.DATA_DIR, settings.RAW_DIR, ..., settings.SHARE_DIR]
    5. FOR dir IN dirs_to_create:
         IF NOT dry_run: dir.mkdir(parents=True, exist_ok=True)
         record created_dirs
    6. files_to_generate = [".env.template", "env_list.conf"]
    7. FOR file IN files_to_generate:
         IF force OR NOT exists: generate_from_template(file)
         record generated_files
    8. IF NOT dry_run: ensure_tasks_db_wal(settings.TASKS_DB_PATH)
    9. UPDATE status=COMPLETED, finished_at=now_ms(), result_summary={...}
    10. RETURN result
    EXCEPTION:
         UPDATE status=FAILED, finished_at=now_ms(), errors=[str(e)]
         RAISE
```

### 3.3 Logging Dual Setup (Entry Points)

```python
# packages/finances-core/src/ft_setup/logging.py
import structlog
from rich.logging import RichHandler
import logging

def configure_logging(mode: Literal["cli", "api"], level: int = logging.INFO) -> None:
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.CallsiteParameterAdder(),
    ]
    
    if mode == "cli":
        # Rich pretty logging to stderr
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(rich_tracebacks=True, markup=True, show_time=False)],
        )
        structlog.configure(
            processors=shared_processors + [structlog.dev.ConsoleRenderer(colors=True)],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:  # api
        # JSON to stdout
        structlog.configure(
            processors=shared_processors + [
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
```

---

## 4. Estructura de Archivos Objetivo

```
packages/finances-core/
├── pyproject.toml
├── src/
│   └── finances_core/
│       ├── __init__.py
│       ├── config.py              # FinancesSettings (Pydantic Settings)
│       ├── schemas/
│       │   ├── __init__.py
│       │   ├── setup.py           # EnvVarSpec, EnvVarDiagnosis, SetupDoctorResponse, InitTaskResult
│       │   └── market_data.py     # (Art. 5 Constitución - para otras tools)
│       └── ft_setup/
│           ├── __init__.py
│           ├── cli.py             # Typer app
│           ├── api.py             # FastAPI router
│           ├── service.py         # Lógica compartida (diagnose, run_init)
│           ├── logging.py         # configure_logging(mode, level)
│           ├── env_list.conf      # TOML fuente de verdad
│           └── templates/
│               ├── .env.template.j2
│               └── env_list.conf.j2
└── share/
    └── tasks.db                   # SQLite WAL (Art. 2)
```

---

## 5. Dependencias (pyproject.toml - finances-core)

```toml
[project]
name = "finances-core"
version = "0.1.0"
dependencies = [
    "pydantic>=2.8",
    "pydantic-settings>=2.3",
    "typer[all]>=0.12",
    "fastapi>=0.111",
    "structlog>=24.1",
    "rich>=13.7",
    "python-dotenv>=1.0",
    "uuid6>=1.11",           # UUID v7
    "polars>=1.6",           # Para futuras tools (Art. 8)
]

[project.optional-dependencies]
dev = ["pytest>=8.2", "pytest-asyncio>=0.23", "httpx>=0.27", "ruff>=0.5", "mypy>=1.10"]
```

---

## 6. Decisiones Arquitectónicas Clave (ADR)

| ADR | Decisión | Justificación |
|-----|----------|---------------|
| ADR-001 | `env_list.conf` en TOML dentro de `ft_setup/templates/` + copiado a `src/ft_setup/` en build | Fuente de verdad versionada, legible, parseable sin deps extra |
| ADR-002 | `FinancesSettings` en `finances_core.config` (paquete shared) | Single source of truth para todas las tools; Pydantic Settings maneja prioridad ENV>.env>default |
| ADR-003 | `doctor` logic en `service.py` (función pura), llamada desde CLI y API | Dualidad real (Art. 1): misma lógica, adaptadores delgados |
| ADR-004 | `init` vía `BackgroundTasks` + `SQLTaskManager` en API; CLI llama directo pero registra en tasks.db | Art. 3 (no bloqueo), Art. 18 (dispatch + poll), Art. 2 (WAL) |
| ADR-005 | Secrets detection por patrón de nombre (heurística) + flag explícito en `env_list.conf` | Pragmático para MVP; evita mantener lista hardcodeada |
| ADR-006 | `uuid6` para UUID v7 (timestamp-ordered) | Art. 18 Constitución; ordenamiento natural en DB/logs |
| ADR-007 | `structlog` configurado en entrypoint (CLI vs API) | Art. 9 Constitución; loggers de librería agnósticos |