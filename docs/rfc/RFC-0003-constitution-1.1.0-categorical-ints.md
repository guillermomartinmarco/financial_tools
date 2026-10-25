# RFC-0003 — Enmienda de la Constitución v1.1.0: enteros categóricos (Art. 5.1 bis) y excepciones de precisión (Art. 5.4)

| Campo | Valor |
|---|---|
| Estado | **Propuesto** — pendiente de aprobación del Tech Lead |
| Fecha | 2026-10-25 |
| Tipo | **Enmienda** (Gobernanza: RFC + aprobación + *bump* de versión) |
| Versión | 1.0.0 → **1.1.0** (MINOR: amplía lo permitido; no rompe compatibilidad de proceso) |
| Artículos | Art. 5.1 (nuevo 5.1 bis) y Art. 5.4 |
| Relacionados | RFC-0001 (aprobado), RFC-0002 (se reduce a la frontera `float64` de TA-Lib) |

## Contexto
1. **Columnas categóricas**: el Art. 5.1 solo admite `float32` e `int64`. Los reconocedores de patrones de TA-Lib
   devuelven categorías discretas (valores medidos: −200, −100, 0, 100, 200), y lo mismo ocurrirá con señales,
   regímenes de mercado y estados que vendrán después. Guardarlas como `float32` oculta su naturaleza y duplica el
   tamaño; resolver cada caso con un RFC de excepción es burocracia repetida.
2. **Contradicción interna**: el Art. 5.3 prevé excepciones de mayor precisión vía RFC y módulo aislado, pero el
   Art. 5.4 prohíbe `float64` de forma absoluta en *feature engineering*. El RFC-0001 (cálculo `Float64` del oráculo
   `ft_indicators`) y el RFC-0002 (frontera `float64` obligatoria de TA-Lib) quedan apoyados en el 5.3 y en
   conflicto con la letra del 5.4.

## Texto propuesto

**Nuevo Art. 5.1 bis — Columnas categóricas y discretas**
> Las columnas cuyo dominio es un conjunto finito de códigos enteros (p. ej. patrones de velas, señales, regímenes,
> estados) **PUEDEN** persistirse como `int8` o `int16`, eligiendo el tipo más pequeño que represente el dominio
> completo. El dominio **DEBE** documentarse en el `plan.md` de la herramienta y validarse al escribir: un valor fuera
> del rango del tipo es un error, nunca se trunca. Las magnitudes continuas siguen el Art. 5.1 (`float32`).

**Art. 5.4 — se agrega al final del primer punto**
> …o kernels CUDA, **salvo las excepciones aprobadas por RFC según el Art. 5.3, que deben quedar aisladas en el módulo
> declarado en el RFC, con un único punto de conversión a los tipos canónicos antes de persistir o transmitir, y
> verificadas por un test automático.**

**Encabezado**: `Versión: 1.1.0`, con una línea de historial de enmiendas que referencie este RFC.

## Efectos
- `ft_ta_lib` persiste patrones/categóricos como `Int16` por el Art. 5.1 bis, sin excepción.
- RFC-0001 y RFC-0002 quedan explícitamente válidos por el Art. 5.4 enmendado.
- RFC-0002 se reduce a la frontera `float64` de TA-Lib.
- Ninguna herramienta existente cambia: hoy nadie persiste enteros categóricos ni usa `float64`.

## Alternativas descartadas
- **RFC de excepción por cada columna categórica**: repetitivo; la regla es general.
- **`float32` para categorías**: cumple la letra, pero oculta el tipo de dato y duplica el tamaño.

## Aprobación
- [ ] Aprobado por el Tech Lead — Firma: ________________ Fecha: __________

Al aprobarse, se aplica el texto en `docs/constitution.md` y se actualiza el resumen de `AGENTS.md`.
