# `rsi2_connors` — Reversión RSI(2) de Larry Connors

Estrategia integrada (`origin = builtin`), solo largo/plano.

## Lógica de negocio

Esta estrategia implementa el enfoque clásico de Larry Connors utilizando un RSI de periodo muy corto (típicamente 2). El objetivo es capturar correcciones rápidas en activos que mantienen una tendencia alcista a largo plazo. Se entra en largo cuando el RSI(2) cae por debajo de un umbral muy bajo (sobreventa profunda) y se sale cuando el RSI(2) sube por encima de un umbral superior, indicando que el impulso de reversión ha terminado.

## Matemática

- **RSI (Wilder)**: Calculado sobre el periodo `period`.
- **Señales**:
    - $\text{entry}[t] = \text{RSI}[t] < \text{lower}$
    - $\text{exit}[t] = \text{RSI}[t] > \text{upper}$

## Diagrama

```mermaid
stateDiagram-v2
    [*] --> Plano
    Plano --> Largo: entry (RSI(2) < lower, sobreventa)
    Largo --> Plano: exit (RSI(2) > upper)
    Largo --> Plano: fin de datos (liquidación final)
```

## Diccionario de parámetros

| Parámetro | Tipo | Rango | Defecto | Descripción |
|---|---|---|---|---|
| `period` | entero | 1..1000 | 2 | Periodo del RSI (clásico de Connors = 2) |
| `lower` | flotante | 0.0..100.0 | 10.0 | Umbral de sobreventa para entrada |
| `upper` | flotante | 0.0..100.0 | 70.0 | Umbral de salida |

## Limitaciones

Al usar un periodo tan corto, la estrategia es muy sensible al ruido del precio y puede generar señales frecuentes de entrada y salida.
