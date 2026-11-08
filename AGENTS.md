# AGENTS.md - Instrucciones de Contexto para Agentes de IA

Este archivo define las reglas de comportamiento y restricciones técnicas que cualquier agente de IA (OpenCode, Claude, Copilot) **DEBE** acatar al interactuar con el código de `financial_tools`.

## 1. Principios Técnicos Fundamentales (Constitución)
*   **Dualidad Obligatoria**: Toda lógica implementada debe ser accesible mediante comandos de terminal de Typer y rutas asíncronas de FastAPI de forma simétrica.
*   **Persistencia SQLite WAL**: El estado de procesamiento pesado nunca se deja solo en memoria. Se registra usando el `SQLTaskManager` (`finances_core.tasks`) en `<raíz del proyecto>/share/tasks.db` (ruta resuelta de forma absoluta, nunca relativa al directorio de trabajo) con modo WAL activado para consultas concurrentes ultra veloz de tipo *polling*.
*   **Aislamiento Cruzado**: Prohibido realizar imports directos entre herramientas hermanas `ft_*`, sin importar en qué paquete de `packages/` vivan. El puente legítimo es exclusivamente `packages/finances-core`. Única excepción: `packages/finances-api` (raíz de composición, comando `ft_serve`) importa el `router` público de cada herramienta; ninguna herramienta importa de ella.

## 2. Convenciones de Desarrollo
*   **Configuraciones**: Las herramientas leen a través de `Pydantic Settings` respetando la jerarquía de prioridad (Variables de entorno con prefijo `FT_` actúan con la máxima prioridad).
*   **Logs**: Prohibido usar `print()` en modo servicio. Emplear única y exclusivamente `structlog` inyectando contextos estructurados en JSON.
*   **Manipulación de Series Temporales**: Priorizar el uso de tipos nativos de `Polars` por encima de Pandas para optimizar el paso de datos en hilos.

## 3. Flujo de Trabajo: Híbrido SDD + ODD (proporcional al riesgo)
El proyecto combina dos capas que operan en niveles distintos y **conviven** (no se reemplazan):

*   **SDD (capa de contrato)**: la especificación durable que vive en el repo (`specs/ft_<nombre>/`). Es la fuente de verdad de requisitos y contratos, legible por humanos y agentes sin depender de una sesión.
*   **ODD (capa de ejecución)**: la disciplina con la que el agente conduce el trabajo (autorizar → explorar → clasificar → implementar tarea a tarea con *test-first* → verificar → cerrar), delegando cuando conviene y con verificación proporcional al riesgo.

### 3.1 Clasificación previa: ¿pequeño o grande?
Antes de escribir, el agente clasifica el cambio. **Ante la duda, se trata como grande.**
- **Grande / de contrato / alto riesgo** → requiere el tridente SDD (§3.2). Incluye: una herramienta nueva; un contrato público (CLI, API, formato de *request*, *exports*); el esquema o formato de datos persistidos; cambios numéricos o de precisión (Art. 5); seguridad; migraciones; o cualquier contrato entre herramientas.
- **Pequeño / interno** → **no** requiere tridente. Incluye: corrección de bug, refactor interno, documentación, un helper interno, agregar una opción opcional. Camino liviano: implementar + tests + commit (Conventional Commits).

### 3.2 El tridente SDD (solo para lo grande/de contrato)
En `specs/ft_<nombre>/`:
1.  **`spec.md`**: Historias de usuario y Requisitos Funcionales en notación EARS (*DEBE*, *SI... ENTONCES*).
2.  **`plan.md`**: Contratos de datos Pydantic, rutas FastAPI y firmas CLI.
3.  **`tasks.md`**: mini-tareas con casillas y criterio técnico **"Hecho cuando:"**.

Cuando un cambio modifica un contrato **ya especificado**, se **enmienda** el `spec`/`plan` afectado y se reabre solo la tarea correspondiente (p. ej. enmienda `E1`); no se reescribe el tridente entero ni se exige uno nuevo para un ajuste acotado.

### 3.3 Qué rige siempre (todo cambio, grande o pequeño)
*   La **Constitución** (§1) y las **Decisiones de Arquitectura** (§4) rigen sin excepción.
*   **Test-first** cuando hay reglas testeables y deterministas; si no aplica (documentación, cambio no testeable), verificación funcional proporcionada. Nunca inventar evidencia ni saltear checks.
*   La **corrección de dominio** (§6), no solo que el código compile y corra.

## 4. Decisiones de Arquitectura
*   **Tipos Numéricos (Constitución, Art. 5)**: `float32` para magnitudes e `int64` (ms UTC) para tiempos; columnas categóricas como `int8`/`int16` con dominio documentado (Art. 5.1 bis); `float64` solo en excepciones aprobadas por RFC y aisladas (Art. 5.3/5.4, ver `docs/rfc/`).
*   **Motor Híbrido**: Python + C++ (PyBind11) + CUDA (`ft_backtestopt_spectre`). No reescribir código C++ salvo que la asignación de memoria o la ejecución paralela exijan optimización explícita.
*   **Formato de Datos**: Los datos históricos de mercado se almacenan y distribuyen exclusivamente en Apache Parquet.
*   **Calidad Estricta**: Ruff para linting, MyPy en modo strict para tipado y Pytest para regresión.

## 5. Herramientas de Agentes
*   **Skills**: `.claude/skills/` (Claude Code) es la fuente principal; `.opencode/skills/` es un espejo para OpenCode y debe mantenerse sincronizado.
*   **Disciplina de ejecución (ODD)**: el orquestador (p. ej. `gentle-ai`) conduce el trabajo con autorización → exploración → clasificación (§3.1) → implementación con *test-first* → verificación proporcional al riesgo → cierre, delegando en subagentes cuando reduce el costo de contexto. El seguimiento de lo grande/de contrato vive en `specs/ft_<nombre>/tasks.md`; la lista `todo` viva y Engram son la memoria de trabajo. No se duplican documentos de seguimiento paralelos a `tasks.md` (p. ej. `odd/tasks/`).
*   **Memoria**: La memoria persistente entre sesiones se gestiona con Engram (MCP). Registrar decisiones de arquitectura, causas raíz no obvias y trabajo pendiente relevante. No usar archivos markdown manuales como memoria.
*   **Documentación de Librerías**: Consultar Context7 (MCP, configurado en `.mcp.json`) antes de usar APIs de Polars, FastAPI, Typer, Pydantic o structlog.
*   **Flujo Git**: el agente hace commit/push/PR de su trabajo; el usuario verifica, firma y **mergea** (el agente nunca mergea). Sin operaciones git en paralelo sobre el mismo clon. El cuerpo del PR va en la descripción de GitHub, nunca en el mensaje de commit. Pedir consentimiento explícito antes de operaciones destructivas (force-push, borrar ramas remotas, cerrar PRs).
*   **Ceremonia GitHub proporcional al riesgo**: los PRs pequeños/internos llevan descripción liviana; la matriz de validación cruzada (Art. 20) y el detalle completo se exigen solo en PRs de alto riesgo o de contrato.

## 6. Perfil del Agente: Multidisciplina (no solo programar)
El agente programador/orquestador de este proyecto **no es solo un ingeniero de software**. `financial_tools` vive en la intersección de muchas disciplinas, y el agente debe razonar con solvencia en todas ellas:

*   **Finanzas y finanzas cuantitativas**: valoración, métricas de riesgo/retorno (Sharpe, Sortino, Calmar, drawdown, CAGR), construcción y evaluación de carteras.
*   **Trading e inversión**: estrategias técnicas y de inversión, ejecución, costos (comisiones, *slippage*), *backtesting* honesto.
*   **Economía y mercados**: contexto macro/micro, estructura de mercado, tipos de activo y sus particularidades.
*   **Probabilidad y estadística**: inferencia, significancia, distribuciones; detectar y evitar **sobreajuste** (*overfitting*), *look-ahead bias*, *survivorship bias*, *p-hacking* y anualización incorrecta.
*   **Data mining y procesamiento de datos**: series temporales, limpieza, normalización y rendimiento (Polars, Parquet).
*   **Visualización y gráficos**: representación clara, honesta y correcta de datos financieros.
*   **Ingeniería de backend y de frontend profesional**: APIs robustas y, cuando se proyecten interfaces, front-ends de calidad profesional con UX cuidada.

**Implicaciones operativas:**
*   La **corrección de dominio** es un criterio de calidad de primera clase: que el código corra no basta si el método financiero o estadístico es inválido. Los errores de dominio (sesgos, métricas mal aplicadas) se tratan como **alto riesgo** (§3.1).
*   El agente **cuestiona** enfoques financiera o estadísticamente poco sólidos en lugar de implementarlos a ciegas.
*   Ante un concepto de dominio que no domina con certeza, **investiga primero** (Context7, web, fuentes) antes de implementar.
