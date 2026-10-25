# AGENTS.md - Instrucciones de Contexto para Agentes de IA

Este archivo define las reglas de comportamiento y restricciones técnicas que cualquier agente de IA (OpenCode, Claude, Copilot) **DEBE** acatar al interactuar con el código de `financial_tools`.

## 1. Principios Técnicos Fundamentales (Constitución)
*   **Dualidad Obligatoria**: Toda lógica implementada debe ser accesible mediante comandos de terminal de Typer y rutas asíncronas de FastAPI de forma simétrica.
*   **Persistencia SQLite WAL**: El estado de procesamiento pesado nunca se deja solo en memoria. Se registra usando el `SQLTaskManager` (`finances_core.tasks`) en `<raíz del proyecto>/share/tasks.db` (ruta resuelta de forma absoluta, nunca relativa al directorio de trabajo) con modo WAL activado para consultas concurrentes ultra veloz de tipo *polling*.
*   **Aislamiento Cruzado**: Prohibido realizar imports directos entre herramientas hermanas `ft_*`, sin importar en qué paquete de `packages/` vivan. El puente legítimo es exclusivamente `packages/finances-core`.

## 2. Convenciones de Desarrollo
*   **Configuraciones**: Las herramientas leen a través de `Pydantic Settings` respetando la jerarquía de prioridad (Variables de entorno con prefijo `FT_` actúan con la máxima prioridad).
*   **Logs**: Prohibido usar `print()` en modo servicio. Emplear única y exclusivamente `structlog` inyectando contextos estructurados en JSON.
*   **Manipulación de Series Temporales**: Priorizar el uso de tipos nativos de `Polars` por encima de Pandas para optimizar el paso de datos en hilos.

## 3. Flujo SDD Mandatorio (Estilo MoureDev)
Antes de generar código de producción, el agente de IA está obligado a validar que exista el tridente de documentos en la carpeta de especificaciones de la herramienta (`specs/ft_<nombre>/`):
1.  **`spec.md`**: Definición de Historias de usuario y Requisitos Funcionales bajo notación EARS formal (*DEBE*, *SI... ENTONCES*).
2.  **`plan.md`**: Contratos de datos de Pydantic, rutas exactas de FastAPI y firmas de CLI.
3.  **`tasks.md`**: Desglose de mini-tareas con casillas de verificación y su respectivo criterio técnico de **"Hecho cuando:"**.

Cualquier prompt que solicite código sin una especificación previa debe ser rechazado educadamente, solicitando primero la redacción del tridente de diseño.

## 4. Decisiones de Arquitectura
*   **Motor Híbrido**: Python + C++ (PyBind11) + CUDA (`ft_backtestopt_spectre`). No reescribir código C++ salvo que la asignación de memoria o la ejecución paralela exijan optimización explícita.
*   **Formato de Datos**: Los datos históricos de mercado se almacenan y distribuyen exclusivamente en Apache Parquet.
*   **Calidad Estricta**: Ruff para linting, MyPy en modo strict para tipado y Pytest para regresión.

## 5. Herramientas de Agentes
*   **Skills SDD**: `.claude/skills/` (Claude Code) es la fuente principal; `.opencode/skills/` es un espejo para OpenCode y debe mantenerse sincronizado.
*   **Flujo Único**: El flujo SDD de la sección 3 es el único flujo de trabajo del proyecto. No se usan orquestadores externos (p. ej. ODD de `gentle-ai`); el seguimiento de avance vive exclusivamente en `specs/ft_<nombre>/tasks.md`.
*   **Memoria**: La memoria persistente entre sesiones se gestiona con Engram (MCP). Registrar decisiones de arquitectura, causas raíz no obvias y trabajo pendiente relevante. No usar archivos markdown manuales como memoria.
*   **Documentación de Librerías**: Consultar Context7 (MCP, configurado en `.mcp.json`) antes de usar APIs de Polars, FastAPI, Typer, Pydantic o structlog.
