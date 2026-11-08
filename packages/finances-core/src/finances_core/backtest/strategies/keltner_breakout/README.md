# `keltner_breakout` — Ruptura del Canal de Keltner

Estrategia integrada (`origin = builtin`), solo largo/plano.

## Lógica de negocio

Esta estrategia utiliza el Canal de Keltner para identificar rupturas de volatilidad. Se entra en largo cuando el precio de cierre rompe al alza la banda superior del canal, lo que indica un fuerte impulso alcista. La posición se mantiene hasta que el precio rompe a la baja la banda inferior del canal, señalando un cambio de tendencia o una caída significativa.

## Matemática

- **Banda Central (Media)**: $\text{middle} = \text{EMA}(\text{close}, n)$
- **Ancho del Canal**: $\text{channel} = k \cdot \text{ATR}(\text{high}, \text{low}, \text{close}, n)$
- **Bandas**:
    - $\text{upper} = \text{middle} + \text{channel}$
    - $\text{lower} = \text{middle} - \text{channel}$
- **Señales**:
    - $\text{entry}[t] = \text{close}[t] > \text{upper}[t]$
    - $\text{exit}[t] = \text{close}[t] < \text{lower}[t]$

## Diagrama

```mermaid
stateDiagram-v2
    [*] --> Plano
    Plano --> Largo: entry (close rompe la banda superior)
    Largo --> Plano: exit (close rompe la banda inferior)
    Largo --> Plano: fin de datos (liquidación final)
```

## Diccionario de parámetros

| Parámetro | Tipo | Rango | Defecto | Descripción |
|---|---|---|---|---|
| `n` | entero | 2..10000 | 20 | Periodo para la EMA y el ATR |
| `k` | flotante | 0.5..10.0 | 2.0 | Multiplicador del ATR para definir el ancho del canal |

## Limitaciones

En mercados laterales, el precio puede cruzar las bandas frecuentemente generando señales falsas (whipsaws).
