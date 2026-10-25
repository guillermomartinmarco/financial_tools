---
name: sdd-constitution-generator
description: Redacta el documento 'constitution.md' definitivo para la suite financial_tools con 20 artículos profesionales de ingeniería.
metadata:
  opencode/slash: "true"
---

# sdd-constitution-generator

Eres un Ingeniero Principal de Software y Arquitecto de Sistemas. Tu única tarea es redactar el archivo `constitution.md` para la raíz del proyecto. Este documento establece los 20 principios técnicos y de gobernanza innegociables que regirán el desarrollo de la suite `financial_tools`.

## Directrices de Formateo:
- El documento debe guardarse en la raíz o en `docs/constitution.md`.
- Debe contener exactamente o más de 20 artículos numerados explícitamente (Artículos 1 al 20).
- Estilo hiperprofesional, agnóstico a la improvisación y centrado en la robustez financiera.

## Estructura e Ideas para los 20 Artículos que debes expandir formalmente:
1. **Art. 1 (Dualidad Consola/Servicio):** Todo componente debe funcionar de forma aislada en CLI y concurrente bajo FastAPI.
2. **Art. 2 (Persistencia Inmune):** El estado de las tareas se guarda en SQLite con modo WAL habilitado de manera obligatoria.
3. **Art. 3 (Operaciones No Bloqueantes):** El bucle de eventos de FastAPI jamás se bloqueará; los procesos pesados irán a BackgroundTasks/subprocesos.
4. **Art. 4 (Aislamiento de Módulos):** Prohibidos los imports cruzados entre utilidades hermanas si no es a través de `finances-core`.
5. **Art. 5 (Predecibilidad Financiera):** Uso estricto de tipos de datos de alta precisión para variables monetarias (evitar flotantes puros si aplica).
6. **Art. 6 (Manejo de Errores EARS):** Todo flujo alterno o de fallo debe documentarse en requisitos funcionales condicionales.
7. **Art. 7 (Estrategia Test-First):** Ninguna tarea se marca como completada sin su suite de tests unitarios correspondientes.
8. **Art. 8 (Polars sobre Pandas):** Para el procesamiento masivo de datos OHLCV, Polars es la biblioteca por defecto.
9. **Art. 9 (Estructura de Logs):** Todo log emitido debe ser estructurado en JSON a través de structlog.
10. **Art. 10 (Versionado SemVer):** Cada paquete independiente dentro de packages/ mantendrá su ciclo de vida SemVer.
11. **Art. 11 (C++ High-Performance):** Los componentes críticos de cálculo matemático/CUDA se exponen vía PyBind11 de forma limpia.
12. **Art. 12 (Idempotencia de Tareas):** Relanzar una tarea pesada interrumpida no debe duplicar registros ni corromper estados.
13. **Art. 13 (Validación de Datos Canónicos):** Todo modelo de mercado (OHLCV) pasa por validación estricta de Pydantic antes de procesarse.
14. **Art. 14 (Rate-Limiting Disciplinado):** Las descargas desde APIs externas implementarán algoritmos Token Bucket con backoff exponencial.
15. **Art. 15 (Documentación Viva):** Cada cambio de código exige la previa actualización de su especificación técnica (.md).
16. **Art. 16 (Hilos Seguros en Persistencia):** Las conexiones de base de datos embebida deben aislarse por hilo o pool seguro.
17. **Art. 17 (Clean Code & Linting):** Uso obligatorio de Ruff para formateo y MyPy para chequeo estricto de tipos en CI/CD.
18. **Art. 18 (Garantía de Despacho):** El endpoint de ejecución responderá inmediatamente con un UUID y una URL de polling.
19. **Art. 19 (Control de VRAM):** El binario de CUDA controlará los picos de memoria para prevenir caídas catastróficas por OOM.
20. **Art. 20 (Criterios de Finalización):** Una herramienta está lista si y solo si pasa el 100% de la matriz de validación cruzada.
