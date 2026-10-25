---
name: ears-rules
description: Fuerza la redacción de criterios de aceptación utilizando la sintaxis estricta de EARS (Easy Approach to Requirements Syntax).
metadata:
  opencode/slash: "false"
---

# ears-rules

Cuando redactes secciones de Requisitos Funcionales o Criterios de Aceptación para `financial_tools`, debes formatear cada regla utilizando exclusivamente patrones EARS. No uses lenguaje ambiguo.

### Patrones Permitidos:
- **Ubicuos (Siempre activos):** El sistema **DEBE** [acción].
- **Prerrequisito:** MIENTRAS [estado del sistema], el sistema **DEBE** [acción].
- **Disparados por Eventos:** CUANDO [evento de disparo], el sistema **DEBE** [acción].
- **Respuestas a Fallos:** SI [condición de error], ENTONCES el sistema **DEBE** [acción].

*Ejemplo:* "SI el token de la API de Binance no está configurado en `main.conf`, ENTONCES el modo servicio DEBE registrar un error crítico y rechazar la inicialización del endpoint."
