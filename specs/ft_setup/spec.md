# Spec — `ft_setup` (MVP v1.0)

## Contexto e Historias de Usuario

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | DevOps / Ingeniero | Como operador de la suite, **quiero** un comando único que inicialice el entorno completo (directorios, configs, validación de secrets) **para que** cualquier máquina quede lista en segundos sin pasos manuales. |
| US-2 | Desarrollador | Como desarrollador, **quiero** ver de un vistazo qué variables `FT_*` espera la suite, cuáles están seteadas y cuáles faltan **para que** no pierda tiempo adivinando configs. |
| US-3 | Servicio (API) | Como `ft_api_service`, **necesito** un endpoint `GET /setup/status` que devuelva el mismo diagnóstico que el CLI **para que** el health-check y la UI de admin puedan mostrar estado de configuración. |
| US-4 | Seguridad | Como auditor, **exijo** que cualquier valor de variable clasificada como *secreto* (API keys, passwords, tokens) **nunca** se loguee ni imprima en claro, sino enmascarado (`***`). |

---

## Requisitos Funcionales (EARS)

### RF-1 — Inicialización de Entorno (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `init` que:
- Cree la estructura de directorios canónica: `data/{raw,processed,parquet}`, `logs/`, `strategies/`, `results/`, `share/` (para `tasks.db`).
- Genere un archivo `.env.template` en la raíz del proyecto con **todas** las variables `FT_*` documentadas (nombre, descripción, ejemplo, requerido: true/false).
- Genere `env_list.conf` en `packages/finances-core/src/ft_setup/` listando las variables canónicas (fuente de verdad para RF-2/RF-3).
- Cree el archivo marcador `.ft_root` en la raíz del proyecto si no existe. Toda ruta relativa de `FinancesSettings` se resuelve contra el primer ancestro con `.ft_root` (o `FT_PROJECT_ROOT`), nunca contra el directorio actual (Art. 2).
- Sea **idempotente**: re-ejecutar no duplica ni corrompe (Art. 12 Constitución).

### RF-2 — Diagnóstico de Variables (Ubícuo)
**EL SISTEMA DEBE** proveer una operación `doctor` (CLI) / `GET /setup/doctor` (API) que:
- Lea `env_list.conf` como fuente canónica de variables esperadas.
- Para cada variable: reporte `nombre`, `valor_actual` (enmascarado si es secreto), `fuente` (env / .env / default / **faltante**), `requerida` (bool).
- Imprima/retorne tabla legible (CLI: Rich table; API: JSON array).
- **SI** una variable requerida está ausente, **ENTONCES** el exit code CLI = 1 y el HTTP status = 200 con campo `ok: false` en respuesta.

### RF-3 — Enmascaramiento de Secretos (Ubícuo)
**EL SISTEMA DEBE** clasificar como *secreto* toda variable cuyo nombre contenga (case-insensitive): `KEY`, `SECRET`, `PASSWORD`, `TOKEN`, `PRIVATE`, `MNEMONIC`, `API_KEY`, `ACCESS_KEY`.
**SI** una variable es secreta, **ENTONCES** su valor **DEBE** mostrarse como `***` en **cualquier** salida (CLI, API, logs structlog, Rich).

### RF-4 — Validación de Tipos y Rangos (Evento-Driven)
**EL SISTEMA DEBE** validar que variables con sufijo `_PORT` sean `int` en rango `[1, 65535]`, `_HOST` sean hostname/IP válido, `_URL` sean URL parseable, `_DIR`/`_PATH` existan o sean creables.
**SI** validación falla, **ENTONCES** reportar error específico con variable, valor recibido, y regla violada.

### RF-5 — Modo Servicio: Endpoint de Diagnóstico (Ubícuo)
**EL SISTEMA DEBE** exponer bajo FastAPI (prefijo `/setup`):
- `GET /setup/doctor` → JSON con diagnóstico completo (mismo dato que CLI `doctor`).
- `POST /setup/init` → Ejecuta inicialización en background task, retorna `task_id` (UUID v7) + `poll_url` (Art. 18 Constitución).
- `GET /setup/status/{task_id}` → Polling de estado de inicialización.

### RF-6 — Logging Dual (Ubícuo)
**EL SISTEMA DEBE** usar `structlog` configurado según modo:
- **CLI**: `rich.logging.RichHandler` (colores, progress bars, tablas bonitas). Nivel por defecto `INFO`, `--verbose`→`DEBUG`, `--quiet`→`WARNING`.
- **API**: JSON estructurado a stdout con `task_id`, `component="ft_setup"`, `trace_id`, `span_id`.

### RF-7 — Persistencia de Estado de Inicialización (Estado-Driven)
**EL SISTEMA DEBE** registrar cada ejecución de `init` (CLI o API) en `SQLTaskManager` (`share/tasks.db` WAL) con estado `PENDING`→`RUNNING`→`COMPLETED`/`FAILED`, guardando: `task_id`, `started_at`, `finished_at`, `result_summary` (JSON), `error` (si aplica).

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Gestión de secrets en vaults externos** (HashiCorp, AWS Secrets Manager, 1Password) | **Fuera de alcance MVP**. Solo enmascaramiento local. RFC futuro. |
| **Edición interactiva de `.env`** (prompts, TUI) | **Fuera de alcance MVP**. Solo lectura y reporte. |
| **Migración de configs legacy** | **Fuera de alcance**. Asumimos greenfield. |
| **Validación de conectividad real a APIs** (probar Binance key, etc.) | **Fuera de alcance MVP**. Solo validación sintáctica/formal. |
| **Generación de certificados TLS / mTLS** | **Fuera de alcance**. Pertenece a `ft_api_service`. |
| **Backup/restore de configuración** | **Fuera de alcance MVP**. |