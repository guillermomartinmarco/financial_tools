# Spec — `ft_strategy_manager` (MVP v1.0)

## Contexto e Historias de Usuario

| ID | Rol | Historia |
|----|-----|----------|
| US-1 | Quant Researcher | Como investigador cuantitativo, **quiero** versionar y persistir mis estrategias (parámetros, hiperparámetros, metadatos) **para que** pueda reproducir experimentos, comparar versiones y auditar decisiones. |
| US-2 | Backtest Engineer | Como ingeniero de backtesting, **necesito** recuperar una estrategia por `strategy_id` y versión **para que** el motor de backtesting (`ft_backtestopt_spectre`, `ft_vectorbt`, etc.) ejecute siempre la configuración exacta declarada. |
| US-3 | DevOps / MLOps | Como operador, **quiero** un endpoint `GET /strategies` y `GET /strategies/{id}/versions` **para que** la UI de gestión y los pipelines CI/CD puedan listar, deployar y promocionar estrategias a producción. |
| US-4 | Auditor / Compliance | Como auditor, **exijo** trazabilidad completa: quién creó/modificó, cuándo, qué cambió (diff), y hash inmutable del contenido **para que** cumpla con requisitos regulatorios de trazabilidad algorítmica. |

---

## Requisitos Funcionales (EARS)

### RF-1 — Crear Estrategia (Ubícuo)
**EL SISTEMA DEBE** proveer operación `create` (CLI: `ft_strategy_manager create`, API: `POST /strategies`) que acepte:
- `name` (str, único, regex `^[a-z][a-z0-9_-]{2,63}$`)
- `description` (str, opcional, max 500 chars)
- `parameters` (dict[str, Any] — JSON schema validado: claves string, valores int/float/bool/str/list/dict anidados)
- `tags` (list[str], opcional, para categorización)
- **Y DEBE** retornar: `strategy_id` (UUID v7), `version: 1`, `created_at_ms` (int64), `hash` (SHA-256 del JSON canónico de parámetros).

### RF-2 — Leer Estrategia (Ubícuo)
**EL SISTEMA DEBE** proveer operación `get` (CLI: `ft_strategy_manager get <id> [--version N]`, API: `GET /strategies/{id}?version=N`) que retorne la estrategia completa con sus parámetros.
**SI** `version` no se especifica, **ENTONCES** retornar la **última versión**.
**SI** `strategy_id` no existe, **ENTONCES** CLI exit code 1 + mensaje; API 404 con body `{detail: "Strategy not found"}`.

### RF-3 — Actualizar Estrategia — Nueva Versión (Ubícuo)
**EL SISTEMA DEBE** proveer operación `update` (CLI: `ft_strategy_manager update <id>`, API: `PUT /strategies/{id}`) que:
- Cree una **nueva versión** (incremental: v1, v2, v3...) **sin mutar** versiones previas (inmutabilidad histórica).
- Acepte `parameters` (dict completo de reemplazo) **O** `parameters_patch` (JSON Patch RFC 6902 para actualizaciones parciales).
- Valide que el resultado pase mismo schema que `create`.
- Retorne `strategy_id`, `new_version`, `new_hash`, `updated_at_ms`.
**SI** `strategy_id` no existe, **ENTONCES** error 404/CLI exit 1.

### RF-4 — Eliminar Estrategia (Ubícuo)
**EL SISTEMA DEBE** proveer operación `delete` (CLI: `ft_strategy_manager delete <id> [--purge]`, API: `DELETE /strategies/{id}?purge=false`) que:
- Por defecto (`purge=false`): **soft-delete** — marca `deleted_at_ms`, oculta de listados normales, pero preserva historial para auditoría.
- Con `--purge` / `purge=true`: **hard-delete** — elimina **todas** versiones y metadatos **irreversiblemente** (requiere confirmación explícita en CLI).
**SI** estrategia no existe, **ENTONCES** idempotente: éxito silencioso (API 204, CLI exit 0).

### RF-5 — Listar Estrategias (Ubícuo)
**EL SISTEMA DEBE** proveer operación `list` (CLI: `ft_strategy_manager list`, API: `GET /strategies`) con filtros:
- `tag` (str, opcional — filtra por tag exacto)
- `name_contains` (str, opcional — substring case-insensitive)
- `include_deleted` (bool, default false)
- `limit` (int, default 50, max 500)
- `offset` (int, default 0)
- Retornar: array de `StrategySummary` {id, name, latest_version, updated_at_ms, tags, hash_latest} + `total_count`.

### RF-6 — Diff de Versiones (Ubícuo)
**EL SISTEMA DEBE** proveer operación `diff` (CLI: `ft_strategy_manager diff <id> --from v1 --to v3`, API: `GET /strategies/{id}/diff?from=1&to=3`) que retorne diff semántico JSON (added/removed/changed keys con old/new values).
**SI** versiones no existen, **ENTONCES** error 404/CLI exit 1.

### RF-7 — Validación de Parámetros contra Schema (Evento-Driven)
**EL SISTEMA DEBE** validar `parameters` en `create`/`update` contra un **JSON Schema** definido por el usuario al crear la estrategia (campo opcional `parameter_schema` en `create`).
**SI** `parameter_schema` existe y `parameters` no validan, **ENTONCES** rechazar con error detallado (JSON Schema validation errors).
**SI** no se provee schema, **ENTONCES** aceptar cualquier JSON object (modo permisivo).

### RF-8 — Persistencia en SQLite WAL (Estado-Driven)
**EL SISTEMA DEBE** almacenar estrategias y versiones en `packages/finances-core/share/strategies.db` (SQLite WAL) con tablas:
- `strategies` (id, name, description, parameter_schema, created_at_ms, deleted_at_ms)
- `strategy_versions` (id, strategy_id, version, parameters_json, hash, created_at_ms, created_by)
- Índices en `strategies.name` (unique), `strategy_versions.strategy_id + version` (unique).

### RF-9 — Logging Dual y Auditoría (Ubícuo)
**EL SISTEMA DEBE** loguear toda operación mutante (`create`, `update`, `delete`) con `structlog` incluyendo: `strategy_id`, `version`, `action`, `user` (si auth futuro), `hash_before`, `hash_after`, `diff_summary`.
- **CLI**: Rich table para `list`/`get`/`diff`; pretty output para `create`/`update`.
- **API**: JSON estructurado a stdout (Art. 9 Constitución).

### RF-10 — Endpoints API Completos (Ubícuo)
**EL SISTEMA DEBE** exponer bajo FastAPI (prefijo `/strategies`):
| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/strategies` | Crear (retorna 201 + Location header) |
| GET | `/strategies` | Listar con query params (RF-5) |
| GET | `/strategies/{id}` | Obtener (query `version` opcional) |
| PUT | `/strategies/{id}` | Nueva versión (RF-3) |
| DELETE | `/strategies/{id}` | Soft/hard delete (query `purge`) |
| GET | `/strategies/{id}/versions` | Listar versiones (paginado) |
| GET | `/strategies/{id}/diff` | Diff entre versiones (query `from`, `to`) |
| GET | `/strategies/{id}/export` | Exportar estrategia + versiones a JSON/YAML |

---

## Casos Límite y Fuera de Alcance (No-Go)

| Tema | Decisión |
|------|----------|
| **Autenticación / Autorización (RBAC, ABAC)** | **Fuera de alcance MVP**. Asumimos entorno de confianza. Header `X-User-ID` opcional para auditoría. |
| **Ejecutar/Backtestear la estrategia** | **Fuera de alcance**. Pertenece a `ft_vectorbt`, `ft_backtestopt_spectre`, `ft_api_service` (orquestación). |
| **Plantillas de estrategia / Wizards interactivos** | **Fuera de alcance MVP**. Solo CRUD de JSON. |
| **GitOps / Sincronización con repo Git** | **Fuera de alcance MVP**. RFC futuro. |
| **Parámetros con tipos financieros complejos (Decimal, Money)** | **Fuera de alcance MVP**. Solo tipos JSON nativos (int/float/bool/str/list/dict). Normalización float32/int64 aplica en *ejecución*, no en *definición*. |
| **Webhooks / Notificaciones de cambios** | **Fuera de alcance MVP**. Polling via API suficiente. |
| **Import/Export masivo (CSV, Excel)** | **Fuera de alcance MVP**. Solo JSON/YAML individual. |