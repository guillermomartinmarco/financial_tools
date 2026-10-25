---
name: sdd-artifacts-generator
description: Genera los archivos spec.md, plan.md y tasks.md para una utilidad específica de la suite financial_tools siguiendo el flujo estricta de MoureDev.
metadata:
  opencode/slash: "true"
---

# sdd-artifacts-generator

Cuando se invoque este comando especificando una sub-tool (Ej: `ft_market_data_binance`), debes generar una carpeta de especificación en `specs/<nombre_de_la_tool>/` y redactar simultáneamente tres archivos cruciales:

## Artefacto 1: `spec.md` (Especificación de Requisitos)
Debe seguir la notación EARS y estructurarse de la siguiente manera:
- **Contexto e Historias de Usuario:** Rol, qué quiere y para qué lo quiere.
- **Requisitos Funcionales (RF):** Numerados secuencialmente (RF-1, RF-2...) aplicando sintaxis formal:
  - *Ubicuos:* "El sistema DEBE..."
  - *Respuestas a Fallos:* "SI [error], ENTONCES el sistema DEBE..."
- **Casos Límite y Fuera de Alcance:** Definir fronteras del código para evitar el Scope Creep.

## Artefacto 2: `plan.md` (Plan de Arquitectura y Decisiones)
Debe traducir la spec en ingeniería pura sin escribir la lógica de negocio final:
- **Modelo de Datos y Contratos:** Definición de clases Pydantic, esquemas de tablas SQLite u objetos Polars.
- **Mapeo de Interfaces:** Firma exacta del CLI (comandos, flags Typer) y firma exacta de la API HTTP (métodos FastAPI, BackgroundTasks).
- **Algoritmos Clave:** Lógica secuencial (ej. paso de polling o flujos asíncronos con el `TaskManager`).

## Artefacto 3: `tasks.md` (Lista Atómica de Tareas)
Debe desglosar el desarrollo en tareas ultra-pequeñas e independientes para desarrollo guiado (Vibe Coding/TDD):
- Formato de lista de Markdown con checkboxes (`- [ ] T1: Descripción`).
- Cada tarea debe incluir obligatoriamente el criterio técnico de aceptación: **"Hecho cuando:"**.

*Nota: Asegúrate de heredar los principios de la constitución del repositorio en cada decisión de diseño.*
