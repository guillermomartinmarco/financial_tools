# RFC-0005 — Enmienda de la Constitución v1.3.0: flujo híbrido SDD + ODD proporcional al riesgo (Art. 7, 15, 20)

| Campo | Valor |
|---|---|
| Estado | **Aprobado** (2026-11-07, Tech Lead) — aplicado en `docs/constitution.md` v1.3.0 |
| Fecha | 2026-11-07 |
| Tipo | **Enmienda** (Gobernanza: RFC + aprobación + *bump* de versión) |
| Versión | 1.2.0 → **1.3.0** (MINOR: relaja y aclara el proceso; no invalida trabajo previo conforme) |
| Artículos | Art. 15 (acotado), Art. 20 (alcance aclarado), Art. 7 (reformulado); nota de Gobernanza |
| Relacionados | Resumen en `AGENTS.md` §3/§5 (flujo híbrido), RFC-0003 (precedente de enmienda) |

## Contexto

El proyecto está avanzado, robusto y funcionando, pero el **SDD obligatorio para todo cambio**
(Art. 15, "Spec-First") impone una ceremonia desproporcionada sobre los cambios pequeños e
internos (correcciones de bug, refactors internos, documentación, helpers), donde el tridente
`spec.md`/`plan.md`/`tasks.md` aporta poco y cuesta mucho de mantener. La misma rigidez pareja
se arrastra a la ceremonia de GitHub (la Matriz del Art. 20 aplicada a cada PR).

A la vez, la **capa de contrato durable** que da el SDD es un activo irremplazable para una
suite financiera/numérica: requisitos EARS, contratos de datos y de API legibles por humanos y
agentes sin depender de una sesión. **No se quiere perder eso.**

La solución no es reemplazar SDD por otra metodología, sino reconocer que **SDD (capa de
contrato) y ODD (capa de ejecución) operan en niveles distintos y conviven**, y hacer la
exigencia **proporcional al riesgo**: tridente para lo de contrato/alto riesgo; camino liviano
(implementar + tests + commit) para lo pequeño/interno. La Constitución (§1, §4) y los
invariantes de arquitectura siguen rigiendo **sin excepción** en ambos caminos.

Esta enmienda es **compatible hacia atrás a nivel de proceso**: todo lo que antes era conforme
lo sigue siendo (se puede seguir redactando el tridente siempre); solo se deja de **exigirlo**
para lo pequeño.

## Texto propuesto

**Art. 7 — Estrategia Test-First (Definición de Hecho)** — reformulado:
> Toda **lógica de servicio con comportamiento testeable y determinista** **DEBE** cubrirse con
> su suite de tests (camino feliz, bordes y al menos un caso de fallo documentado en EARS,
> Art. 6) **antes de considerarse terminada**, exista o no una tarea en `tasks.md`. En el
> trabajo trackeado, ninguna tarea de `tasks.md` se marca **Done** sin su suite. Cuando una
> regla no es testeable de forma determinista (documentación, cambio no funcional) se aplica
> **verificación funcional proporcionada**, nunca evidencia inventada ni checks salteados.
> Cobertura mínima objetivo: **90%** sobre la lógica de servicio (no adaptadores).

**Art. 15 — Documentación Viva (Spec-First, proporcional al riesgo)** — acotado:
> La especificación técnica (`spec.md`, `plan.md`, `tasks.md` en `specs/ft_<herramienta>/`) es
> **contrato, no apéndice**: la fuente de verdad durable de requisitos y contratos.
> - **Cambios de contrato o de alto riesgo** —herramienta nueva; contrato público de
>   CLI/API/formato de *request*/*exports*; esquema o formato de datos persistidos; cambios
>   numéricos o de precisión (Art. 5); seguridad; migraciones; o contratos entre herramientas—
>   **EXIGEN** la actualización **previa** de la especificación: **no hay *merge* sin spec al
>   día**. Si el cambio modifica un contrato ya especificado, se **enmienda** el `spec`/`plan`
>   afectado y se reabre solo la tarea correspondiente; no se reescribe el tridente entero.
> - **Cambios pequeños o internos** —corrección de bug, refactor interno, documentación,
>   helper interno, opción opcional— **NO EXIGEN** tridente, pero **NO DEBEN** contradecir la
>   especificación vigente. Si la contradicen, dejan de ser pequeños y recaen en el punto
>   anterior.
> - **Ante la duda** sobre si un cambio toca un contrato, se trata como de contrato.

**Art. 20 — Criterios de Finalización (Matriz de Validación Cruzada 100%)** — se antepone a la tabla:
> La matriz es el **criterio de madurez de una herramienta**: se evalúa al declarar una
> `ft_<nombre>` **lista para producción**, y en ese hito debe pasar al **100%**. **No es un
> requisito por cada *pull request***. En un *pull request* individual se evidencian únicamente
> las **dimensiones que el cambio toca**, con detalle **proporcional al riesgo**: los PRs
> pequeños/internos llevan evidencia liviana; los de contrato o alto riesgo documentan las
> dimensiones afectadas y, cuando corresponde, la matriz completa.

(La tabla del Art. 20 y el criterio "SI Y SOLO SI 100%" para la madurez de la herramienta se mantienen sin cambios.)

**Gobernanza y Enmiendas** — aclaración para mantenedor único:
> **Enmiendas:** Requieren RFC documentado, aprobación del **Tech Lead** (en un equipo,
> consenso del *core*) y *bump* de versión del documento (`MAJOR` si rompe compatibilidad hacia
> atrás de proceso).

**Encabezado de `constitution.md`**: `Versión: 1.3.0`, con una línea de historial que referencie este RFC.

## Efectos

- Un bugfix, un refactor interno, un helper o un cambio de documentación **ya no exigen** redactar el tridente: se implementan, se testean (si hay lógica testeable) y se commitean.
- Lo de contrato/alto riesgo (motor numérico, contratos CLI/API, esquemas de datos) **sigue** con tridente o con enmienda de spec; no se pierde protocolo ni contrato de datos.
- La Matriz del Art. 20 deja de inflar cada PR; se reserva para la madurez de herramienta y para PRs de contrato/alto riesgo.
- Ningún trabajo previo deja de ser conforme; las herramientas ya listas para producción lo siguen estando.
- `AGENTS.md` (§3 flujo híbrido, §5 disciplina ODD + git + ceremonia proporcional, §6 perfil multidisciplina) queda alineado con esta constitución; se corrige la referencia de versión del §4.

## Alternativas descartadas

- **Reemplazar SDD por ODD por completo**: perdería la capa de contratos durables (requisitos EARS, contratos de datos), crítica en un dominio financiero/numérico; además los specs quedarían abandonados y driftearían. Descartada.
- **Mantener el SDD obligatorio para todo**: es la ceremonia que motiva este RFC; desproporcionada para lo pequeño.
- **Relajar `AGENTS.md` sin enmendar la constitución**: dejaría el repo inconsistente (Art. 15 "Excepciones: Ninguna" vs. camino liviano) y la constitución ganaría. Descartada.

## Aprobación

- [x] Aprobado por el Tech Lead — Firma: Guillermo Martín Marco Fecha: 07/11/2026

Al aprobarse, se aplica el texto en `docs/constitution.md` (v1.3.0) y queda sincronizado el resumen de `AGENTS.md`.
