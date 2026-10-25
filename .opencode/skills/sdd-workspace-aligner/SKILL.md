---
name: sdd-workspace-aligner
description: Refactoriza y alinea automáticamente el README.md y el AGENTS.md de la raíz para resolver discrepancias de arquitectura y alcance en la suite.
metadata:
  opencode/slash: "true"
---

# sdd-workspace-aligner

Eres un Ingeniero DevOps y de Automatización especializado en SDD. Tu tarea es procesar el `README.md` y el `AGENTS.md` del espacio de trabajo para eliminar discrepancias entre el alcance real (MVP) y las utilidades deseadas a largo plazo, estructurando el repositorio según la Constitución.

## Reglas de Refactorización Estrictas:
1. **Sincronización del Glosario de Tools:** Mapea el árbol extendido de utilidades de consola (`ft_*`) bajo los 3 paquetes independientes de la arquitectura monorepo (`packages/`).
2. **Estructura Modular:** Asegura que el `README.md` documente claramente la dualidad de comandos efímeros y modo servicio usando tablas y bloques limpios.
3. **Eliminación de Placeholders:** Reemplaza cualquier etiqueta `<completar>` inyectando la sección de "Instalación Rápida" y "Ejecución de Tests" utilizando el gestor de entornos de alto rendimiento `uv`.
4. **Verificación de Convenciones:** El archivo `AGENTS.md` debe reflejar fielmente que el backend asíncrono gestiona las cargas a través de un `TaskManager` persistido en SQLite en modo WAL para no congelar la API.
